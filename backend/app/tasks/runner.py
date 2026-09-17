"""任务状态流转与后台执行。

状态语义：
- `uploading`：原始视频尚未落对象存储（由上传接口维护）。
- `uploaded`：已落对象存储，等待处理，执行器可认领。
- `processing`：已被某个执行器认领并写入 `process_token`。
- `succeeded` / `failed`：终态；失败可由重试接口退回 `uploaded` 重新入队。

任务体（V0.1.4 起）为媒体探测：用上传留下的本地副本喂给 ffprobe，结果写入任务行。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.media import ProbeError, original_path, probe_file, remove_original, sha256_file
from app.models import Task, UploadSession, utcnow
from app.storage import StorageError, describe_error, get_storage

logger = logging.getLogger(__name__)

TASK_KIND_INGEST = "ingest"

STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"
STATUS_PROCESSING = "processing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})

# 进度语义：认领 0 → 探测中 PROGRESS_PROBING → 入库完成 100。探测是单一不可切分的步骤，
# 不给更细的百分比，避免造出「看起来精确、实际拍脑袋」的进度。
PROGRESS_PROBING = 20
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

    任务体为媒体探测（V0.1.4）：探测失败与入库失败一律落库为明确原因，并保留本地副本，
    便于在容器内直接用同一份文件复跑 ffprobe 复现问题。
    """
    task = claim_task(db, task_id)
    if task is None:
        logger.info("任务 %s 未能认领（可能已被处理或状态不符），跳过", task_id)
        return

    try:
        _process(db, task)
    except Exception as exc:  # noqa: BLE001 —— 任务失败必须落库，不能让线程静默退出
        logger.exception("任务 %s 执行失败", task_id)
        task.status = STATUS_FAILED
        task.error = _failure_reason(exc)
        task.updated_at = utcnow()
        db.commit()
        return

    task.status = STATUS_SUCCEEDED
    task.progress = PROGRESS_DONE
    task.error = None
    task.updated_at = utcnow()
    db.commit()
    logger.info("任务 %s 处理完成", task_id)


def _failure_reason(exc: BaseException) -> str:
    """把异常转成可入库的失败原因；存储异常保留 request_id 等诊断信息。"""
    if isinstance(exc, (ProbeError, StorageError)):
        return f"{type(exc).__name__}: {describe_error(exc)}"
    return f"{type(exc).__name__}: {exc}"


def _prepare_local_source(task: Task) -> Path:
    """准备待探测的本地文件：优先用上传留下的副本，缺失时从对象存储取回。

    上传成功但进程在探测前退出时本地副本仍在；副本已被清理（如上一轮探测成功后又重试）
    则回下载，使「首次执行」与「重试」共用同一段逻辑。
    """
    settings = get_settings()
    path = original_path(settings.media_root, task.id, task.filename)

    if path.is_file():
        logger.info("任务 %s 使用本地副本探测：%s", task.id, path.name)
        return path

    if not task.object_key:
        raise ProbeError("任务缺少原始视频的对象键，也没有本地副本，无法探测")

    logger.info("任务 %s 本地副本缺失，从对象存储取回：%s", task.id, task.object_key)
    get_storage().download_to_file(task.object_key, path)
    return path


def _process(db: Session, task: Task) -> None:
    """任务体：探测原始视频并把元数据写入任务行，成功后清理本地副本。"""
    source = _prepare_local_source(task)

    task.progress = PROGRESS_PROBING
    task.updated_at = utcnow()
    db.commit()

    settings = get_settings()
    metadata = probe_file(
        source,
        ffprobe_path=settings.ffprobe_path,
        timeout_seconds=settings.probe_timeout_seconds,
        max_output_bytes=settings.probe_max_output_bytes,
    )
    content_hash = sha256_file(source)

    _store_metadata(task, metadata, content_hash)
    db.commit()
    logger.info("任务 %s 探测完成：%s", task.id, metadata.summary())

    # 探测成功即释放本地副本：后续版本需要原视频时按对象键取回，不必长期占用磁盘
    remove_original(settings.media_root, task.id, task.filename)


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

    上一次探测可能已经写入部分元数据（例如探测成功但后续步骤失败），重试前清空，
    避免失败的任务仍展示上一次的陈旧结论，让「有元数据」等价于「本次探测成功」。
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
    """删除任务行与其上传会话行；先删子行再删父行，避免外键残留。

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
