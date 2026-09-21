"""上传入库链路：会话状态取值与「合并 → 上传对象存储 → 建立待处理任务」。

本模块只处理已落盘分片，不感知 HTTP：接口层（`app/routers/uploads.py`）与启动恢复
（`app/tasks/executor.py`）共用这里的 `finalize`。抽出来的直接原因是「关掉页面后上传
必须还能完成」——分片与状态都已落盘，续跑不需要浏览器参与，因此不能把它写在请求处理里。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app import chunks as chunk_store
from app.config import Settings
from app.media.paths import original_path, remove_file
from app.models import Task, UploadSession, utcnow
from app.storage import OBJECT_KIND_ORIGINAL, StorageError, describe_error, get_storage
from app.tasks.runner import (
    STATUS_FAILED as TASK_STATUS_FAILED,
    STATUS_UPLOADED,
    STATUS_UPLOADING as TASK_STATUS_UPLOADING,
)

logger = logging.getLogger(__name__)

# 会话状态取值：uploading 收分片、assembling 合并入库中、completed 已完成、failed 失败可续传
STATUS_UPLOADING = "uploading"
STATUS_ASSEMBLING = "assembling"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# 与任务状态同名不同义（任务的 `uploading` 指「还没入库」，会话的指「还在收分片」），
# 因此导入方一律用带 SESSION_ 前缀的别名，避免读代码时把两者当成同一个取值
SESSION_UPLOADING = STATUS_UPLOADING
SESSION_ASSEMBLING = STATUS_ASSEMBLING
SESSION_COMPLETED = STATUS_COMPLETED

ACCEPTING_STATUSES = (STATUS_UPLOADING, STATUS_FAILED)
# 启动恢复时值得尝试续跑的状态：completed 已终态，其余三种都可能有「分片齐了但没人合并」
RESUMABLE_STATUSES = (STATUS_UPLOADING, STATUS_ASSEMBLING, STATUS_FAILED)


class UploadFinalizeError(Exception):
    """入库失败：原因已经写入会话与任务行，调用方只需决定如何对外呈现。"""


def reopen_if_failed(db: Session, session: UploadSession, task: Task) -> None:
    """失败后重新提交视为继续本次上传：会话退回 uploading 并清掉旧的失败原因。

    分片产物保留在磁盘上，因此重试不需要重传已成功的分片。任务在原始视频真正落存储前
    一律退回 `uploading`，避免「还没入库的任务」被判为可重试。
    """
    if session.status != STATUS_FAILED:
        return
    session.status = STATUS_UPLOADING
    session.error = None
    session.updated_at = utcnow()
    task.status = TASK_STATUS_UPLOADING
    task.error = None
    task.process_token = None
    task.updated_at = utcnow()
    db.commit()


def finalize(db: Session, session: UploadSession, task: Task, settings: Settings) -> str:
    """把已收齐的分片合并、上传对象存储，并把任务置为待处理；返回对象键。

    可重复执行：对象键已确定（存储里已有原始视频）或本地副本已在时，直接沿用不再重传，
    只把状态推到 `uploaded`。启动恢复会把中途中断的会话再跑一遍，重复的后果是桶里多出
    一份没人引用的 GB 级对象与一次多余的几十分钟上传，因此这里必须幂等。

    **本地视频副本在入库成功后即释放**（V0.6.1）：探测与切分改从对象存储按需取回，本地
    磁盘不再囤积视频。任务被认领前若进程重启，副本会由启动时的残留清扫收走。

    失败原因写入会话与任务两处，并抛出 `UploadFinalizeError`。
    """
    object_key = task.object_key
    local_copy = original_path(settings.media_root, task.id, session.filename)

    try:
        if object_key:
            # 上一轮已入库，只是状态没推完（例如进程在提交前退出）
            _drop_merged(settings, session)
        elif local_copy.is_file():
            # 上一轮已合并但未入库：本地副本就是那份完整文件，不必再从分片拼一次
            logger.info("上传会话 %s 复用已有本地副本：%s", session.id, local_copy.name)
            object_key = _upload_copy(local_copy, session.filename)
            _drop_chunks(settings, session)
        else:
            total = chunk_store.chunk_count(session.declared_size, session.chunk_size)
            merged = chunk_store.assemble(
                settings.media_root, session.id, total, session.declared_size
            )
            object_key = _upload_copy(merged, session.filename)
            # 合并产物转存为「待探测的本地副本」：探测直接用它，不再从对象存储拉 GB 级文件
            chunk_store.move_merged_to_original(merged, local_copy)
            _drop_chunks(settings, session)
    except (chunk_store.UploadError, StorageError) as exc:
        # 失败时保留分片（用户回来续传不必重传 GB 级原文件），但合并临时文件必须清掉：
        # 它是这份素材的第二份完整副本，留着既占盘又对续跑没有帮助
        _drop_merged(settings, session)
        _mark_failed(db, session, task, exc)
        raise UploadFinalizeError(describe_error(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 未知异常同样要落库，避免会话永远卡在 assembling
        logger.exception("上传会话 %s 合并或入库失败", session.id)
        _drop_merged(settings, session)
        _mark_failed(db, session, task, exc)
        raise UploadFinalizeError(describe_error(exc)) from exc

    # 体积必须在删副本之前取：删完再 stat 只能拿到默认值
    size = local_copy.stat().st_size if local_copy.is_file() else session.declared_size

    session.status = STATUS_COMPLETED
    session.error = None
    session.updated_at = utcnow()
    task.object_key = object_key
    task.size = size
    task.status = STATUS_UPLOADED
    task.progress = 0
    task.error = None
    # 上次失败可能已写入 token，重新入队前必须清掉，否则认领条件不成立
    task.process_token = None
    task.updated_at = utcnow()
    db.commit()

    # 对象已在存储中就位，本地那份完整副本完成使命（V0.6.1 起不再保留）
    _drop_local_copy(settings, task, session)

    return object_key


def _upload_copy(path, filename: str | None) -> str:
    """上传一份完整的原始视频，返回对象键。"""
    storage = get_storage()
    object_key = storage.generate_object_key(OBJECT_KIND_ORIGINAL, filename or "")
    return storage.upload_file(path, object_key).object_key


def _drop_chunks(settings: Settings, session: UploadSession) -> None:
    """对象已在存储中就位，本地分片与合并临时文件完成使命。"""
    try:
        chunk_store.cleanup(settings.media_root, session.id)
    except OSError as exc:  # noqa: BLE001 —— 清理失败不改变「已入库」的结论
        logger.warning("上传会话 %s 分片清理失败：%s", session.id, exc)


def _drop_local_copy(settings: Settings, task: Task, session: UploadSession) -> None:
    """删除入库成功后遗留的本地原始副本。

    入库本身可能被启动恢复重跑，因此这里失败只记 warning：副本残留由启动清扫兜底，
    不该因为一次 unlink 失败就把已经成功的入库判死。
    """
    path = original_path(settings.media_root, task.id, session.filename)
    remove_file(path, description="入库后的本地原始副本")


def _drop_merged(settings: Settings, session: UploadSession) -> None:
    """只清合并临时文件：分片留着没有意义，但也不必为一次重复的恢复去删目录。"""
    chunk_store.cleanup_merged(settings.media_root, session.id)


def _mark_failed(db: Session, session: UploadSession, task: Task, exc: Exception) -> None:
    """把失败原因同时写入上传会话与任务，保证两个入口都能看到原因。"""
    message = f"{type(exc).__name__}: {describe_error(exc)}"
    session.status = STATUS_FAILED
    session.error = message
    session.updated_at = utcnow()
    # 对象已落存储时任务不该被判死：原始视频仍可用，重试接口可重新执行
    task.status = STATUS_UPLOADED if task.object_key else TASK_STATUS_FAILED
    task.error = message
    task.updated_at = utcnow()
    db.commit()
    logger.error("上传会话 %s 失败：%s", session.id, message)
