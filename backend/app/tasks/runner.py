"""任务状态流转与后台执行。

状态语义：
- `uploading`：原始视频尚未落对象存储（由上传接口维护）。
- `uploaded`：已落对象存储，等待处理，执行器可认领。
- `processing`：已被某个执行器认领并写入 `process_token`。
- `succeeded` / `failed`：终态；失败可由重试接口退回 `uploaded` 重新入队。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Task, utcnow

logger = logging.getLogger(__name__)

TASK_KIND_INGEST = "ingest"

STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"
STATUS_PROCESSING = "processing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})


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

    本版任务体为空壳：探测属 V0.1.4、切分属 V0.1.5，此处只完成状态流转，
    使「关闭页面后任务继续推进」与「失败可见、可重试」可独立验证。
    """
    task = claim_task(db, task_id)
    if task is None:
        logger.info("任务 %s 未能认领（可能已被处理或状态不符），跳过", task_id)
        return

    try:
        _process(task)
    except Exception as exc:  # noqa: BLE001 —— 任务失败必须落库，不能让线程静默退出
        logger.exception("任务 %s 执行失败", task_id)
        task.status = STATUS_FAILED
        task.error = f"{type(exc).__name__}: {exc}"
        task.updated_at = utcnow()
        db.commit()
        return

    task.status = STATUS_SUCCEEDED
    task.progress = 100
    task.error = None
    task.updated_at = utcnow()
    db.commit()


def _process(task: Task) -> None:
    """任务体占位：本版不处理媒体，仅确认原始视频的入库信息完整。"""
    if not task.object_key:
        raise RuntimeError("任务缺少原始视频的对象键，无法继续处理")
    logger.info("任务 %s 已记录原始视频 %s（%s 字节）", task.id, task.object_key, task.size)


def reset_task_for_retry(db: Session, task_id: str) -> Task | None:
    """把失败任务退回待处理状态；其他状态不予重试。"""
    result = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == STATUS_FAILED)
        .values(status=STATUS_UPLOADED, process_token=None, progress=0, error=None, updated_at=utcnow())
    )
    db.commit()
    if result.rowcount == 0:
        return None
    return db.get(Task, task_id)


def list_tasks(db: Session, limit: int = 20) -> list[Task]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars())
