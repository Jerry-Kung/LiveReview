"""任务接口：查询状态与失败原因、重新发起失败任务、删除已上传视频。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.deletion import delete_task_assets
from app.models import Task
from app.schemas import TaskListResponse, TaskResponse
from app.storage import StorageError, describe_error
from app.tasks import (
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_UPLOADING,
    list_tasks,
    reset_task_for_retry,
    submit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# 这两个状态下后台执行器可能正在读写对象，删除会造成半途而废的清理
BUSY_STATUSES = (STATUS_UPLOADING, STATUS_PROCESSING)


def _to_response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        kind=task.kind,
        status=task.status,
        progress=task.progress,
        filename=task.filename,
        object_key=task.object_key,
        size=task.size,
        error=task.error,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _get_or_404(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return task


@router.get("", response_model=TaskListResponse)
def list_all(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> TaskListResponse:
    """任务列表：关闭页面后回到页面仍可看到历史任务与当前进度。"""
    return TaskListResponse(items=[_to_response(task) for task in list_tasks(db, limit)])


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskResponse:
    """查询单个任务的状态、进度与失败原因。"""
    return _to_response(_get_or_404(db, task_id))


@router.post("/{task_id}/retry", response_model=TaskResponse)
def retry_task(task_id: str, db: Session = Depends(get_db)) -> TaskResponse:
    """重新执行失败任务。

    只有失败的任务可以重试，避免重复处理同一产物；原始视频尚未落存储的任务不属于
    可重试范畴（应重新发起上传），因此其状态在完成上传前不会进入「失败」以外的可重试态。
    """
    task = _get_or_404(db, task_id)
    if task.status != STATUS_FAILED or task.object_key is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，只有已入库且失败的任务可以重新执行",
        )

    reset = reset_task_for_retry(db, task_id)
    if reset is None:
        # 并发下可能已被其他请求重置或执行
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="任务状态已变化，请刷新后重试")

    submit(task_id)
    logger.info("任务 %s 已重新入队", task_id)
    return _to_response(reset)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    """删除已上传的视频：清除对象存储中的原始视频、本地分片与任务记录。

    删除不可撤销，因此失败时一律保留记录并返回原因；重复删除返回 404（已不存在）。
    """
    task = _get_or_404(db, task_id)
    if task.status in BUSY_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，上传或处理进行中，无法删除",
        )

    try:
        delete_task_assets(db, task, settings.media_root)
    except StorageError as exc:
        db.rollback()
        message = describe_error(exc)
        logger.error("任务 %s 删除失败：%s", task_id, message)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"删除失败：{message}"
        ) from exc
