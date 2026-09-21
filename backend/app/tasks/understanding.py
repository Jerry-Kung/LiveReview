"""片段识别链路：把切片交给音画理解模型，产出带原视频时间的内容记录。

设计要点：

- **识别与切分解耦**：识别只依赖「片段已在对象存储且有对象键」，因此 V0.1 已完成切分的
  任务可以直接识别；重试单片也不会触发重新切分或重新上传。
- **输入用预签名 URL**：片段按 `tos://` 对象键存放，发给模型的是现签的 HTTPS 地址。
  URL 在**发请求前**生成，且有效期覆盖排队与单次识别耗时，长队列下不会出现过期地址。
- **时间基准是原视频时间轴**：模型给的是片段内相对秒数，落库与展示一律用绝对时间，
  片段起点由 `MediaClip.start_seconds` 提供；越界时段只标记不裁切。
- **失败按片隔离**：单片失败只登记该片状态与原因，其余片段继续识别——整场识别里一片
  失败不该让已经成功的片段作废。已完成的片段在重跑时跳过。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.llm import (
    UNDERSTANDING_SKIPPED,
    UNDERSTANDING_STATUS_FAILED,
    UNDERSTANDING_STATUS_PENDING,
    UNDERSTANDING_STATUS_RUNNING,
    UNDERSTANDING_STATUS_SUCCEEDED,
    LLMClientError,
    LLMError,
    UnderstandingClient,
    align_segments,
    build_prompt,
    describe_error,
    get_understanding_client,
)
from app.models import (
    PROGRESS_UNDERSTANDING_START,
    MediaClip,
    Task,
    utcnow,
)
from app.storage import StorageError, describe_error as describe_storage_error, get_storage

logger = logging.getLogger(__name__)

# 识别进度语义：认领 0 → 逐片推进 5~95 → 汇总完成 100。与切分进度分开，互不覆盖。
# 起点 `PROGRESS_UNDERSTANDING_START` 定义在 `app/models.py`：入队接口与本处的状态迁移
# 是同一处取值（`Task.mark_understanding_running()`），常量随之落到模型层。
PROGRESS_UNDERSTANDING_END = 95
PROGRESS_UNDERSTANDING_DONE = 100


def run_understanding(
    db: Session,
    task_id: str,
    *,
    client: UnderstandingClient | None = None,
    settings: Settings | None = None,
) -> None:
    """对任务的全部可识别片段执行一次识别，返回时任务级状态已落库。

    任务级状态只反映**本次执行**：重跑会先把上一轮的结论清空，避免「上一次成功、
    这一次全失败」却仍显示成功。
    """
    settings = settings or get_settings()
    task = db.get(Task, task_id)
    if task is None:
        logger.warning("识别请求的任务不存在：%s", task_id)
        return

    clips = _clips_of(db, task_id)
    ready = [clip for clip in clips if clip.is_uploaded]
    pending = [clip for clip in ready if not clip.understanding_succeeded]

    if not ready:
        _finish_task(
            db,
            task,
            status=UNDERSTANDING_SKIPPED,
            error="没有已入库的切片可供识别，请先完成切分",
        )
        return

    if not pending:
        # 全部片段都已有识别结果：不重复计费，只把任务级结论按既有结果重算一次
        _summarize(db, task, ready)
        return

    task.mark_understanding_running()
    db.commit()

    try:
        client = client or get_understanding_client()
    except LLMClientError as exc:
        # 配置缺失属于「整场都做不了」：直接判失败，避免逐片各失败一次刷屏
        _finish_task(db, task, status=UNDERSTANDING_STATUS_FAILED, error=describe_error(exc))
        logger.error("任务 %s 识别无法开始：%s", task_id, describe_error(exc))
        return

    prompt = build_prompt(settings.llm_understanding_prompt)
    concurrency = max(1, settings.llm_concurrency)

    if concurrency == 1:
        for position, clip in enumerate(pending):
            _understand_clip(db, task, clip, client=client, prompt=prompt, settings=settings)
            _advance(db, task, position + 1, len(pending))
    else:
        _understand_concurrently(db, task, pending, client=client, prompt=prompt, settings=settings)

    _summarize(db, task, ready)


def retry_clip_understanding(
    db: Session,
    task_id: str,
    index: int,
    *,
    client: UnderstandingClient | None = None,
    settings: Settings | None = None,
) -> MediaClip | None:
    """单独重试一片的识别；已成功的片段不会被重复请求。

    返回更新后的片段行；片段不存在或不可识别时返回 None，由路由层转成明确的接口错误。
    """
    settings = settings or get_settings()
    task = db.get(Task, task_id)
    if task is None:
        return None

    clip = _clip_of(db, task_id, index)
    if clip is None or not clip.is_uploaded:
        return None

    try:
        client = client or get_understanding_client()
    except LLMClientError as exc:
        clip.understanding_status = UNDERSTANDING_STATUS_FAILED
        clip.understanding_error = describe_error(exc)
        clip.understanding_finished_at = utcnow()
        db.commit()
        return clip

    prompt = build_prompt(settings.llm_understanding_prompt)
    _understand_clip(db, task, clip, client=client, prompt=prompt, settings=settings)

    # 单片重试同样刷新任务级汇总：整场状态必须跟着单片结果走，否则界面会自相矛盾
    _summarize(db, task, _clips_of(db, task_id))
    return clip


def _understand_concurrently(
    db: Session,
    task: Task,
    clips: list[MediaClip],
    *,
    client: UnderstandingClient,
    prompt: str,
    settings: Settings,
) -> None:
    """并发识别：HTTP 调用放在工作线程，URL 生成与落库留在主线程。

    数据库会话不是线程安全的，因此工作线程只做「发请求 + 解析」，不碰 Session；
    请求前生成 URL、返回后落库都在主线程串行完成。
    """
    completed = 0
    with ThreadPoolExecutor(
        max_workers=max(1, settings.llm_concurrency), thread_name_prefix="understand"
    ) as pool:
        futures = {}
        for clip in clips:
            video_url = _presigned_url(clip, settings)
            if video_url is None:
                _mark_failed(db, clip, "片段对象地址生成失败，无法识别")
                completed += 1
                _advance(db, task, completed, len(clips))
                continue
            _mark_running(db, clip)
            futures[pool.submit(_call_client, client, video_url, prompt)] = clip

        for future, clip in futures.items():
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 —— 单片失败不应中断其余片段
                _mark_failed(db, clip, _reason(exc))
            else:
                _store_result(db, clip, result, result.attempts)
            completed += 1
            _advance(db, task, completed, len(clips))


def _call_client(client: UnderstandingClient, video_url: str, prompt: str):
    """工作线程入口：只发请求与解析，不接触数据库会话。"""
    return client.understand(video_url=video_url, prompt=prompt)


def _understand_clip(
    db: Session,
    task: Task,
    clip: MediaClip,
    *,
    client: UnderstandingClient,
    prompt: str,
    settings: Settings,
) -> None:
    """识别单个片段：URL 现签现用，失败只登记在本片段上。"""
    video_url = _presigned_url(clip, settings)
    if video_url is None:
        _mark_failed(db, clip, "片段对象地址生成失败，无法识别")
        return

    _mark_running(db, clip)
    try:
        result = client.understand(video_url=video_url, prompt=prompt)
    except Exception as exc:  # noqa: BLE001 —— 单片失败登记原因后继续下一片
        _mark_failed(db, clip, _reason(exc))
        logger.warning("任务 %s 片段 %d 识别失败：%s", task.id, clip.index, _reason(exc))
        return

    _store_result(db, clip, result, result.attempts)


def _presigned_url(clip: MediaClip, settings: Settings) -> str | None:
    """为片段现签一个下载地址；生成失败返回 None（由调用方标记该片失败）。"""
    if not clip.object_key:
        return None
    try:
        return get_storage().generate_presigned_url(
            clip.object_key, expires_seconds=settings.llm_clip_url_ttl_seconds
        )
    except StorageError as exc:
        logger.error("片段 %d 预签名地址生成失败：%s", clip.index, describe_storage_error(exc))
        return None


def _mark_running(db: Session, clip: MediaClip) -> None:
    """置为进行中并立即提交：长任务执行期间界面要能看到「这一片正在识别」。"""
    clip.understanding_status = UNDERSTANDING_STATUS_RUNNING
    clip.understanding_started_at = utcnow()
    clip.understanding_finished_at = None
    clip.understanding_error = None
    db.commit()


def _store_result(db: Session, clip: MediaClip, result: Any, attempts: int) -> None:
    """把一次识别结果落库：绝对时间条目 + 原始文本 + 用量 + 告警。

    `attempts` 是本次实际发出的请求次数（含退避重试），随结果一起留档：重试是否被触发过
    是排查「模型不稳」的关键线索。
    """
    aligned = align_segments(
        result,
        clip_start_seconds=clip.start_seconds,
        clip_end_seconds=clip.end_seconds,
    )
    payload = [
        {
            "start_seconds": round(item.start_seconds, 3),
            "end_seconds": round(item.end_seconds, 3),
            "content": item.content,
            "tone": item.tone,
            "clip_start_seconds": round(item.clip_start_seconds, 3),
            "clip_end_seconds": round(item.clip_end_seconds, 3),
            "out_of_range": item.out_of_range,
        }
        for item in aligned
    ]

    clip.understanding_segments_json = json.dumps(payload, ensure_ascii=False)
    clip.understanding_raw_text = result.raw_text or None
    clip.understanding_usage_json = (
        json.dumps(result.usage, ensure_ascii=False) if result.usage else None
    )
    clip.understanding_warnings_json = (
        json.dumps(result.warnings, ensure_ascii=False) if result.warnings else None
    )
    clip.understanding_attempts = attempts
    clip.understanding_status = UNDERSTANDING_STATUS_SUCCEEDED
    clip.understanding_error = None
    clip.understanding_finished_at = utcnow()
    db.commit()

    logger.info(
        "片段 %d 识别完成：%d 条语音记录，%d 条解析告警",
        clip.index,
        len(payload),
        len(result.warnings),
    )


def _mark_failed(db: Session, clip: MediaClip, reason: str, attempts: int = 0) -> None:
    clip.understanding_status = UNDERSTANDING_STATUS_FAILED
    clip.understanding_error = reason
    clip.understanding_attempts = attempts
    clip.understanding_finished_at = utcnow()
    db.commit()


def _reason(exc: BaseException) -> str:
    if isinstance(exc, LLMError):
        return f"{type(exc).__name__}: {describe_error(exc)}"
    return f"{type(exc).__name__}: {exc}"


def _advance(db: Session, task: Task, done: int, total: int) -> None:
    if total <= 0:
        return
    span = PROGRESS_UNDERSTANDING_END - PROGRESS_UNDERSTANDING_START
    task.understanding_progress = PROGRESS_UNDERSTANDING_START + int(span * done / total)
    task.updated_at = utcnow()
    db.commit()


def _summarize(db: Session, task: Task, clips: list[MediaClip]) -> None:
    """按片段结果汇总任务级状态：只要有一片失败，整场就是「部分完成」。"""
    succeeded = [clip for clip in clips if clip.understanding_succeeded]
    failed = [
        clip
        for clip in clips
        if clip.understanding_status == UNDERSTANDING_STATUS_FAILED
    ]
    segments = 0
    for clip in succeeded:
        segments += len(_load_segments(clip))

    task.understanding_clip_count = len(succeeded)
    task.understanding_segment_count = segments
    task.understanding_finished_at = utcnow()
    task.understanding_progress = PROGRESS_UNDERSTANDING_DONE
    task.understanding_status = (
        UNDERSTANDING_STATUS_FAILED if failed else UNDERSTANDING_STATUS_SUCCEEDED
    )
    if failed:
        first = failed[0]
        task.understanding_error = (
            f"{len(failed)}/{len(clips)} 个片段识别失败，首个失败片段 #{first.index}："
            f"{first.understanding_error or '原因未记录'}"
        )
    else:
        task.understanding_error = None
    task.updated_at = utcnow()
    db.commit()

    logger.info(
        "任务 %s 识别结束：成功 %d 片 / 失败 %d 片，共 %d 条语音记录",
        task.id,
        len(succeeded),
        len(failed),
        segments,
    )


def _finish_task(db: Session, task: Task, *, status: str, error: str | None = None) -> None:
    """任务级终态落库；不改动任何片段状态。"""
    task.understanding_status = status
    task.understanding_error = error
    task.understanding_progress = (
        PROGRESS_UNDERSTANDING_DONE if status != UNDERSTANDING_SKIPPED else 0
    )
    task.understanding_finished_at = utcnow()
    task.updated_at = utcnow()
    db.commit()


def reset_task_understanding(db: Session, task_id: str) -> Task | None:
    """按顺序重跑整场识别：清空任务级结论，片段结论由识别链路按「已成功则跳过」处理。

    `force=True` 的场景（模型或提示词换了）需要连片段结论一并清空，这里不提供：
    重跑前先确认是否要保留既有记录，避免一次误点把已识别人工核对过的结果抹掉。
    """
    task = db.get(Task, task_id)
    if task is None:
        return None
    task.understanding_status = UNDERSTANDING_STATUS_PENDING
    task.understanding_error = None
    task.understanding_progress = 0
    task.understanding_clip_count = 0
    task.understanding_segment_count = 0
    task.understanding_started_at = None
    task.understanding_finished_at = None
    task.updated_at = utcnow()
    db.commit()
    return task


def _load_segments(clip: MediaClip) -> list[dict[str, Any]]:
    """读取片段的语音记录；内容损坏时返回空列表并记日志，不让查询接口整体失败。"""
    if not clip.understanding_segments_json:
        return []
    try:
        payload = json.loads(clip.understanding_segments_json)
    except ValueError:
        logger.warning("片段 %d 的识别记录无法解析，按空处理", clip.index)
        return []
    return payload if isinstance(payload, list) else []


def list_clips(db: Session, task_id: str) -> list[MediaClip]:
    """按序号升序返回任务的切片；路由层与汇总层共用同一处查询。"""
    stmt = select(MediaClip).where(MediaClip.task_id == task_id).order_by(MediaClip.index)
    return list(db.execute(stmt).scalars())


def _clips_of(db: Session, task_id: str) -> list[MediaClip]:
    return list_clips(db, task_id)


def _clip_of(db: Session, task_id: str, index: int) -> MediaClip | None:
    stmt = select(MediaClip).where(
        MediaClip.task_id == task_id, MediaClip.index == index
    )
    return db.execute(stmt).scalars().first()


__all__ = [
    "PROGRESS_UNDERSTANDING_DONE",
    "PROGRESS_UNDERSTANDING_END",
    "PROGRESS_UNDERSTANDING_START",
    "list_clips",
    "reset_task_understanding",
    "retry_clip_understanding",
    "run_understanding",
]
