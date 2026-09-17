"""任务状态流转与后台执行。

状态语义：
- `uploading`：原始视频尚未落对象存储（由上传接口维护）。
- `uploaded`：已落对象存储，等待处理，执行器可认领。
- `processing`：已被某个执行器认领并写入 `process_token`。
- `succeeded` / `failed`：终态；失败可由重试接口退回 `uploaded` 重新入队。

任务体（V0.1.5 起）为「探测 → 转封装 → 切分 → 上传 → 覆盖校验 → 清理」：

- 探测决定时间轴基准，切分依赖它，因此两者串在同一个任务里，一次上传对应一次处理。
- 转封装把 TS 等容器统一为 MP4，切分只针对 MP4 进行。
- 片段切出一片即上传一片并删除本地文件，避免 GB 级片段长期占用磁盘。
- 重试按「产物是否存在」续跑：已上传的片段跳过，转换产物存在则不重复转换。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.media import (
    ClipPlan,
    FFmpegError,
    ProbeError,
    SplitError,
    check_coverage,
    clip_filename,
    clip_path,
    converted_path,
    cut_clip,
    issues_to_json,
    original_path,
    plan_clips,
    prepare_split_source,
    probe_file,
    refine_by_size,
    remove_clip,
    remove_clips_tree,
    remove_file,
    remove_original,
    remove_task_media,
    sha256_file,
    summarize,
)
from app.models import (
    CLIP_STATUS_FAILED,
    CLIP_STATUS_PENDING,
    CLIP_STATUS_UPLOADED,
    MediaClip,
    Task,
    UploadSession,
    utcnow,
)
from app.storage import StorageError, describe_error, get_storage

logger = logging.getLogger(__name__)

TASK_KIND_INGEST = "ingest"

STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"
STATUS_PROCESSING = "processing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})

# 进度语义：认领 0 → 探测完成 20 → 转封装完成 25 → 片段推进 40~98 → 全部完成 100。
# 探测是一步到位，转封装与切片按片段数推进；进度只落在真实完成的步骤上，不造拍脑袋的百分比。
PROGRESS_CLAIMED = 0
PROGRESS_PROBED = 20
PROGRESS_CONVERTED = 25
PROGRESS_SPLIT_START = 40
PROGRESS_SPLIT_END = 98
PROGRESS_DONE = 100


def claim_task(db: Session, task_id: str) -> Task | None:
    """认领任务：仅在 `uploaded` 状态下写入新 token。

    用带条件的 UPDATE 保证并发下只有一个执行器成功认领，返回认领到的任务（否则 None）。
    """
    token = uuid.uuid4().hex[:16]
    result = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == STATUS_UPLOADED)
        .values(status=STATUS_PROCESSING, process_token=token, progress=0, error=None, updated_at=utcnow())
    )
    db.commit()
    if result.rowcount == 0:
        return None
    return db.get(Task, task_id)


def run_task(db: Session, task_id: str) -> None:
    """认领并执行任务，失败时把原因写回任务。

    任何一步失败都落库为明确原因；已上传的片段与转换产物保留，便于重试续跑与在容器内复现。
    """
    task = claim_task(db, task_id)
    if task is None:
        logger.info("任务 %s 未能认领（可能已被处理或状态不符），跳过", task_id)
        return

    try:
        _process(db, task)
    except Exception as exc:  # noqa: BLE001 —— 任务失败必须落库，不能让线程静默退出
        logger.exception("任务 %s 执行失败", task_id)
        db.rollback()
        task = db.get(Task, task_id) or task
        task.status = STATUS_FAILED
        task.error = _failure_reason(exc)
        task.updated_at = utcnow()
        _mark_unfinished_clips(db, task, _failure_reason(exc))
        db.commit()
        return

    task.status = STATUS_SUCCEEDED
    task.progress = PROGRESS_DONE
    task.error = None
    task.updated_at = utcnow()
    db.commit()
    logger.info("任务 %s 处理完成", task.id)


def _failure_reason(exc: BaseException) -> str:
    """把异常转成可入库的失败原因；存储异常保留 request_id 等诊断信息。"""
    if isinstance(exc, (ProbeError, FFmpegError, SplitError, StorageError)):
        return f"{type(exc).__name__}: {describe_error(exc)}"
    return f"{type(exc).__name__}: {exc}"


def _mark_unfinished_clips(db: Session, task: Task, reason: str) -> None:
    """把尚未上传的片段标为失败并把原因写进去，使「哪一片没过」在接口与页面上可见。"""
    for clip in _clips_of(db, task.id):
        if clip.status != CLIP_STATUS_UPLOADED:
            clip.status = CLIP_STATUS_FAILED
            clip.error = reason


def _prepare_local_source(task: Task) -> Path:
    """准备待处理的本地文件：优先用上传留下的副本，缺失时从对象存储取回。

    上传成功但进程在探测前退出时本地副本仍在；副本已被清理（如探测成功后又清理过）
    则回下载，使「首次执行」与「重试」共用同一段逻辑。
    """
    settings = get_settings()
    path = original_path(settings.media_root, task.id, task.filename)

    if path.is_file():
        logger.info("任务 %s 使用本地副本：%s", task.id, path.name)
        return path

    if not task.object_key:
        raise ProbeError("任务缺少原始视频的对象键，也没有本地副本，无法处理")

    logger.info("任务 %s 本地副本缺失，从对象存储取回：%s", task.id, task.object_key)
    get_storage().download_to_file(task.object_key, path)
    return path


def _process(db: Session, task: Task) -> None:
    """任务体：探测 → 转封装 → 切分上传 → 覆盖校验 → 清理本地产物。"""
    settings = get_settings()
    source = _prepare_local_source(task)

    metadata = _ensure_probed(db, task, source, settings)

    task.progress = PROGRESS_PROBED
    task.updated_at = utcnow()
    db.commit()

    split_source, split_metadata, converted = _ensure_converted(db, task, source, metadata, settings)

    clips: list[MediaClip] = []
    try:
        clips = _split_and_upload(db, task, split_source, split_metadata, settings)
    finally:
        # 切片产物是中间态：上传成功的片段文件已即时删除，这里兜住失败与未上传的残片，
        # 避免下一轮把残片当成有效片段复用（原始副本与转换产物保留，便于复现）。
        _cleanup_clip_files(task, clips)
    _remove_clips_dir(task)
    _verify_coverage(db, task, split_metadata.duration_seconds, clips)

    # 切分成功即释放中间产物：后续版本需要片段从对象存储取回，原始视频仍留在桶里
    remove_original(settings.media_root, task.id, task.filename)
    if converted:
        remove_file(converted_path(settings.media_root, task.id), description="转封装产物")


def _ensure_probed(db: Session, task: Task, source: Path, settings):
    """探测原始视频并把结论写入任务行。

    每次都重新探测：`reset_task_for_retry` 会清空探测结论，「有元数据」必须等价于「本次
    处理成功」，因此不复用上一轮的字段。探测本身是流式读取，相对转封装与切分的开销很小。
    """
    metadata = probe_file(
        source,
        ffprobe_path=settings.ffprobe_path,
        timeout_seconds=settings.probe_timeout_seconds,
        max_output_bytes=settings.probe_max_output_bytes,
    )
    _store_metadata(task, metadata, sha256_file(source))
    db.commit()
    logger.info("任务 %s 探测完成：%s", task.id, metadata.summary())
    return metadata


def _ensure_converted(db: Session, task: Task, source: Path, metadata, settings):
    """准备切分输入：已是 MP4 直接用，否则转封装；已有转换产物则复用。

    返回 `(切分输入路径, 该文件的元数据, 是否本次发生了转换)`。
    """
    target = converted_path(settings.media_root, task.id)

    # 重试场景：上一轮已转封装成功且产物还在，直接复用，不必再花几十分钟复制一遍
    if target.is_file():
        converted_metadata = probe_file(
            target,
            ffprobe_path=settings.ffprobe_path,
            timeout_seconds=settings.probe_timeout_seconds,
            max_output_bytes=settings.probe_max_output_bytes,
        )
        logger.info("任务 %s 复用已有转封装产物：%s", task.id, target.name)
        _store_split_source(db, task, converted_metadata)
        return target, converted_metadata, True

    split_source, split_metadata, converted = prepare_split_source(
        source,
        target,
        original_metadata=metadata,
        ffmpeg_command=settings.ffmpeg_path,
        ffprobe_command=settings.ffprobe_path,
        timeout_seconds=settings.split_timeout_seconds,
        probe_timeout_seconds=settings.probe_timeout_seconds,
        max_output_bytes=settings.ffmpeg_max_output_bytes,
    )
    _store_split_source(db, task, split_metadata)
    task.progress = PROGRESS_CONVERTED
    task.updated_at = utcnow()
    db.commit()
    return split_source, split_metadata, converted


def _store_split_source(db: Session, task: Task, metadata) -> None:
    """记录切分输入的时间基准：容器的时长差异会让覆盖校验出现假缺口，必须留痕。"""
    task.split_source_format = metadata.format_name
    task.split_source_duration_seconds = metadata.duration_seconds
    task.converted_at = utcnow()
    task.updated_at = utcnow()
    db.commit()


def _split_and_upload(
    db: Session, task: Task, split_source: Path, split_metadata, settings
) -> list[MediaClip]:
    """按双约束切分并逐片上传，返回本次处理完成后的片段列表（按序号）。

    体积约束需要实测才知道结果，因此这里分两轮：先切一遍拿到每片实际体积，把超出上限的
    片段在原位置对半后重切，再上传。**上传前先把体积约束收敛掉**，避免先传上去一批超限
    片段再留下需要清理的桶内垃圾。
    """
    duration = split_metadata.duration_seconds
    if duration is None:
        raise SplitError("切分输入无法读出时长，无法确定切分范围")

    existing = {clip.index: clip for clip in _reuse_plan(db, task)}
    reused_sizes = _measured_sizes(list(existing.values()))

    clips = plan_clips(
        duration,
        max_duration_seconds=settings.split_max_duration_seconds,
        max_clip_bytes=settings.split_max_clip_bytes,
        min_clip_seconds=settings.split_min_clip_seconds,
        measured=reused_sizes,
    )
    logger.info(
        "任务 %s 切分计划：%d 片，总时长 %.1fs（单片上限 %ds / %d 字节）",
        task.id,
        len(clips),
        duration,
        settings.split_max_duration_seconds,
        settings.split_max_clip_bytes,
    )

    records = _sync_clip_records(db, task, clips, existing)

    # 第一轮：切分并按实测体积收敛，尚不上传（收敛过程会重建计划与片段行）
    clips = _converge_on_size(db, task, clips, records, split_source, settings)

    # 取回收敛后的片段行：收敛可能把片段对半，序号与行都已变化
    records = {clip.index: clip for clip in _clips_of(db, task.id)}

    # 第二轮：逐片上传（此时每个片段都已满足时长与体积约束）
    for position, planned in enumerate(clips):
        record = records[planned.index]
        if record.status == CLIP_STATUS_UPLOADED and record.object_key:
            logger.info("片段 %d 已上传过，跳过", planned.index)
            _advance_progress(db, task, position + 1, len(clips))
            continue
        _upload_clip(db, task, record, planned, settings, position, len(clips))

    return [records[planned.index] for planned in clips]


def _converge_on_size(
    db: Session,
    task: Task,
    clips: list[ClipPlan],
    records: dict[int, MediaClip],
    split_source: Path,
    settings,
) -> list[ClipPlan]:
    """切分并按实测体积细化，直到每个片段都不超过体积上限。

    每轮把切好的片段实测体积喂回 `refine_by_size`，超限片段在原位置对半后再切一遍。收敛后
    返回最终计划；上一轮的残片在下一轮开始前清理，避免与新一轮同名文件混淆。
    """
    max_rounds = 8  # 每次对半至少减半，8 轮足以从 1GB 收敛到 4MB；超出说明参数不合理
    for round_index in range(max_rounds):
        measured: list[tuple[float, float, int]] = []
        sizes: dict[int, int] = {}

        for planned in clips:
            if _is_already_uploaded(records, planned):
                continue
            local_path = clip_path(settings.media_root, task.id, planned.index)
            metadata = cut_clip(
                split_source,
                local_path,
                planned=planned,
                ffmpeg_command=settings.ffmpeg_path,
                ffprobe_command=settings.ffprobe_path,
                timeout_seconds=settings.split_timeout_seconds,
                probe_timeout_seconds=settings.probe_timeout_seconds,
            )
            sizes[planned.index] = local_path.stat().st_size
            measured.append((planned.start_seconds, planned.end_seconds, local_path.stat().st_size))
            records[planned.index].duration_seconds = metadata.duration_seconds
            db.commit()

        oversized = [
            planned
            for planned in clips
            if sizes.get(planned.index, 0) > settings.split_max_clip_bytes
        ]
        if not oversized:
            return clips

        logger.info(
            "任务 %s 第 %d 轮：%d 个片段超过体积上限，对半后重切",
            task.id,
            round_index + 1,
            len(oversized),
        )
        refined = refine_by_size(
            clips,
            max_clip_bytes=settings.split_max_clip_bytes,
            measured=measured,
            min_clip_seconds=settings.split_min_clip_seconds,
        )
        if len(refined) == len(clips):
            raise SplitError(
                f"{len(oversized)} 个片段超过体积上限 {settings.split_max_clip_bytes} 字节，"
                "但已无法继续细分，请检查 SPLIT_MIN_CLIP_SECONDS 与源素材码率"
            )

        _cleanup_clip_files(task, clips)
        clips = refined
        records = _sync_clip_records(db, task, clips, records)

    raise SplitError(
        f"体积约束在 {max_rounds} 轮内未能收敛，请检查 SPLIT_MAX_CLIP_BYTES 是否过小"
    )


def _is_already_uploaded(records: dict[int, MediaClip], planned: ClipPlan) -> bool:
    record = records.get(planned.index)
    return record is not None and record.status == CLIP_STATUS_UPLOADED and bool(record.object_key)


def _upload_clip(
    db: Session,
    task: Task,
    record: MediaClip,
    planned: ClipPlan,
    settings,
    position: int,
    total: int,
) -> None:
    """上传已切好的片段并推进进度；本地文件在上传成功后即删。"""
    local_path = clip_path(settings.media_root, task.id, planned.index)
    if not local_path.is_file():
        raise SplitError(
            f"片段 {planned.index} 的本地文件不存在，无法上传（{local_path.name}）"
        )

    record.size_bytes = local_path.stat().st_size
    # 对象键在首次生成后固定，重试复用同一个键，避免同一片段在桶里留下多份副本
    if not record.object_key:
        record.object_key = get_storage().generate_object_key(
            "clip", clip_filename(planned.index)
        )
    db.commit()

    try:
        uploaded = get_storage().upload_file(local_path, record.object_key)
    except StorageError as exc:
        record.status = CLIP_STATUS_FAILED
        record.error = describe_error(exc)
        task.updated_at = utcnow()
        db.commit()
        raise

    record.status = CLIP_STATUS_UPLOADED
    record.size_bytes = uploaded.size or record.size_bytes
    record.error = None
    db.commit()
    logger.info("片段 %d 已上传：%s", planned.index, record.object_key)

    remove_clip(settings.media_root, task.id, planned.index)
    _advance_progress(db, task, position + 1, total)


def _advance_progress(db: Session, task: Task, done: int, total: int) -> None:
    if total <= 0:
        return
    span = PROGRESS_SPLIT_END - PROGRESS_SPLIT_START
    task.progress = PROGRESS_SPLIT_START + int(span * done / total)
    task.updated_at = utcnow()
    db.commit()


def _reuse_plan(db: Session, task: Task) -> list[MediaClip]:
    """已有的片段行：重试时按序号复用，已上传的片段不必重新切分与上传。"""
    return _clips_of(db, task.id)


def _clips_of(db: Session, task_id: str) -> list[MediaClip]:
    stmt = select(MediaClip).where(MediaClip.task_id == task_id).order_by(MediaClip.index)
    return list(db.execute(stmt).scalars())

def _measured_sizes(existing: list[MediaClip]) -> list[tuple[float, float, int]]:
    """已实测过的 `(起, 止, 字节数)`：供体积超限的片段递归对半。"""
    return [
        (clip.start_seconds, clip.end_seconds, clip.size_bytes)
        for clip in existing
        if clip.size_bytes is not None
    ]


def _sync_clip_records(
    db: Session, task: Task, plans: list[ClipPlan], previous: dict[int, MediaClip]
) -> dict[int, MediaClip]:
    """让片段行与切分计划一一对应，位置不变的片段行**原地更新**而不是删了重建。

    计划会因体积收敛或参数调整而整体改变，因此按「序号 + 起止时间」判断是否同一片：

    - 序号与区间都不变：保留原行与原对象键，已上传的片段在收敛与重试时都不会被重传。
    - 序号不变但区间变了（收敛对半后）：原地改写区间并重置为待处理，对象键作废。
    - 计划缩小时多出来的序号：删除该行。

    原地更新而非「删除 + 插入」是刻意的：`(task_id, index)` 上有唯一约束，同一事务内先插
    后删会撞约束，而 SQLAlchemy 一次 flush 内的 DELETE/INSERT 顺序无法保证。序号是行的
    唯一标识，本来就没有换行的必要。
    """
    keep: dict[int, MediaClip] = {}
    stale_keys: list[str] = []

    for planned in plans:
        record = previous.get(planned.index)
        if record is None:
            record = MediaClip(
                task_id=task.id,
                index=planned.index,
                start_seconds=planned.start_seconds,
                end_seconds=planned.end_seconds,
                status=CLIP_STATUS_PENDING,
            )
            db.add(record)
            keep[planned.index] = record
            continue

        if not _same_range(record, planned):
            # 区间变了说明是另一片：旧对象键作废，桶里的旧对象也要删掉，避免留下孤儿片段
            if record.object_key:
                stale_keys.append(record.object_key)
            record.start_seconds = planned.start_seconds
            record.end_seconds = planned.end_seconds
            record.duration_seconds = None
            record.size_bytes = None
            record.object_key = None
            record.status = CLIP_STATUS_PENDING
            record.error = None
        elif record.status != CLIP_STATUS_UPLOADED:
            record.status = CLIP_STATUS_PENDING
            record.error = None

        keep[planned.index] = record

    for index, record in previous.items():
        if index not in keep:
            if record.object_key:
                stale_keys.append(record.object_key)
            db.delete(record)

    db.flush()
    db.commit()

    # 对象清理放在事务提交之后：删桶失败不影响切分结论，只留下一个可以人工回收的孤儿对象
    if stale_keys:
        _delete_objects_quietly(stale_keys)
    return keep


def _delete_objects_quietly(object_keys: list[str]) -> None:
    """尽力删除作废的对象；失败只记 warning，不让切分结果被回收动作拖垮。"""
    for object_key in object_keys:
        try:
            get_storage().delete_object(object_key)
        except StorageError as exc:
            logger.warning("作废片段对象删除失败：%s（%s）", object_key, describe_error(exc))


def _same_range(record: MediaClip, planned: ClipPlan) -> bool:
    return (
        abs(record.start_seconds - planned.start_seconds) < 0.001
        and abs(record.end_seconds - planned.end_seconds) < 0.001
    )


def _cleanup_clip_files(task: Task, clips: list[MediaClip]) -> None:
    """清掉本地切片残片。

    正常情况下片段在上传成功后即删；这里的清理兜住两种情况：上传失败的片段与计划中尚未
    处理到的片段。任务结束时无论成败都不该留下残片——下一轮会按对象键重新切分，残片只会
    让磁盘与「这份文件是不是有效片段」的判断都变脏。
    """
    settings = get_settings()
    for clip in clips:
        remove_clip(settings.media_root, task.id, clip.index)


def _remove_clips_dir(task: Task) -> None:
    """整目录清理任务的切片产物。

    逐个删除片段文件会漏掉同目录下的其它残留（如失败时写了一半的文件），而下一轮会按相同
    序号重新切分，目录里的任何残留都可能是骗过「这份文件是否有效」判断的脏数据。
    """
    settings = get_settings()
    remove_clips_tree(settings.media_root, task.id)


def _verify_coverage(db: Session, task: Task, duration: float | None, clips: list[MediaClip]) -> None:
    """整场覆盖校验：结论入库；发现问题即判失败，让缺口以「任务失败 + 原因」暴露出来。"""
    issues = check_coverage(duration, [(clip.start_seconds, clip.end_seconds) for clip in clips])
    task.coverage_issues_json = issues_to_json(issues)
    task.split_checked_at = utcnow()
    task.updated_at = utcnow()
    db.commit()

    logger.info("任务 %s %s", task.id, summarize(issues))
    if issues:
        raise SplitError(summarize(issues))


def _store_metadata(task: Task, metadata, content_hash: str) -> None:
    """把探测结果写入任务行：常用字段升列，完整 JSON 另存一处。"""
    video = metadata.video
    audio = metadata.audio

    task.duration_seconds = metadata.duration_seconds
    task.media_format = metadata.format_name
    task.video_codec = video.codec_name if video else None
    task.width = video.width if video else None
    task.height = video.height if video else None
    task.frame_rate = video.frame_rate if video else None
    task.audio_codec = audio.codec_name if audio else None
    task.sample_rate = audio.sample_rate if audio else None
    task.channels = audio.channels if audio else None
    task.stream_count = metadata.stream_count
    task.media_bit_rate = metadata.bit_rate
    task.content_hash = content_hash
    task.metadata_json = metadata.raw_json
    task.probed_at = utcnow()
    task.updated_at = utcnow()


def reset_task_for_retry(db: Session, task_id: str) -> Task | None:
    """把失败任务退回待处理状态；其他状态不予重试。

    上一次执行可能已经写入部分元数据（例如探测成功但切分失败），重试前清空探测结果与覆盖
    结论，避免失败的任务仍展示上一次的陈旧结论，让「有元数据」等价于「本次处理成功」。

    **片段行保留**：它们是「已上传产物」的唯一线索，删掉会让重试重复上传全部片段。片段行
    在任务体里按「序号 + 起止时间」与切分计划比对后复用。
    """
    result = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == STATUS_FAILED)
        .values(
            status=STATUS_UPLOADED,
            process_token=None,
            progress=0,
            error=None,
            updated_at=utcnow(),
            duration_seconds=None,
            media_format=None,
            video_codec=None,
            width=None,
            height=None,
            frame_rate=None,
            audio_codec=None,
            sample_rate=None,
            channels=None,
            stream_count=None,
            media_bit_rate=None,
            content_hash=None,
            metadata_json=None,
            probed_at=None,
            split_source_format=None,
            split_source_duration_seconds=None,
            coverage_issues_json=None,
            split_checked_at=None,
        )
    )
    db.commit()
    if result.rowcount == 0:
        return None
    return db.get(Task, task_id)


def reclaim_stale_processing(db: Session) -> int:
    """把进程重启时残留的 `processing` 任务退回 `uploaded`，使其可被重新认领。

    进程被杀（容器重启、OOM）时任务会停在 `processing`，认领条件不成立，`requeue_pending`
    找不到它，任务就永远卡住。token 是内存态的认领凭据，重启后一定失效，因此这里一律重置。
    """
    result = db.execute(
        update(Task)
        .where(Task.status == STATUS_PROCESSING)
        .values(
            status=STATUS_UPLOADED,
            process_token=None,
            progress=0,
            error=None,
            updated_at=utcnow(),
        )
    )
    db.commit()
    return result.rowcount or 0


def remove_task_records(db: Session, task: Task) -> None:
    """删除任务行与其上传会话行；片段行由 `Task.clips` 的级联一并删除。

    会话与任务一并删除：上传会话对用户没有独立意义，留下只会成为孤儿记录。
    """
    upload_id = task.upload_id
    db.delete(task)
    db.flush()
    if upload_id:
        session = db.get(UploadSession, upload_id)
        if session is not None:
            db.delete(session)
    db.commit()


def list_tasks(db: Session, limit: int = 20) -> list[Task]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars())
