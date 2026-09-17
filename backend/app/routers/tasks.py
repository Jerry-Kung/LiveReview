"""任务接口：查询状态与失败原因、重新发起失败任务、删除已上传视频。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.deletion import delete_task_assets
from app.media import issues_from_json
from app.models import CLIP_STATUS_UPLOADED, Task
from app.schemas import (
    CoverageIssueResponse,
    TaskClipResponse,
    TaskCoverageResponse,
    TaskListResponse,
    TaskMetadataResponse,
    TaskResponse,
)
from app.storage import StorageError, describe_error, get_storage
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


def _to_response(task: Task, settings: Settings) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        kind=task.kind,
        status=task.status,
        progress=task.progress,
        filename=task.filename,
        object_key=task.object_key,
        size=task.size,
        error=task.error,
        metadata=_to_metadata(task),
        coverage=_to_coverage(task),
        clips=_to_clips(task, settings),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _to_clips(task: Task, settings: Settings) -> list[TaskClipResponse]:
    """片段列表：按序号升序，即原视频时间轴上的顺序。

    预签名地址只在片段确有对象键时生成；生成失败（凭据/网络问题）不应让整个任务查询接口
    失败——片段本身的信息仍然有效，因此降级为不提供下载地址并记日志。
    """
    clips: list[TaskClipResponse] = []
    for clip in task.clips:
        download_url = None
        if clip.is_uploaded and clip.object_key:
            try:
                download_url = get_storage().generate_presigned_url(clip.object_key)
            except StorageError as exc:
                logger.warning("片段 %s 预签名地址生成失败：%s", clip.object_key, describe_error(exc))
        clips.append(
            TaskClipResponse(
                index=clip.index,
                start_seconds=clip.start_seconds,
                end_seconds=clip.end_seconds,
                duration_seconds=clip.duration_seconds,
                size_bytes=clip.size_bytes,
                status=clip.status,
                error=clip.error,
                object_key=clip.object_key,
                download_url=download_url,
            )
        )
    return clips


def _to_coverage(task: Task) -> TaskCoverageResponse | None:
    """切分与覆盖校验结论：未校验过则为 null，不把「尚未切分」渲染成「校验通过」。"""
    if not task.has_coverage_check:
        return None
    return TaskCoverageResponse(
        clip_count=len(task.clips),
        checked_at=task.split_checked_at,
        issues=[
            CoverageIssueResponse(**issue) for issue in issues_from_json(task.coverage_issues_json)
        ],
        source_format=task.split_source_format,
        source_duration_seconds=task.split_source_duration_seconds,
    )


def _to_metadata(task: Task) -> TaskMetadataResponse | None:
    """探测结果只在真正探测成功过时返回：避免把「未探测」渲染成一份空元数据。"""
    if not task.has_metadata:
        return None
    return TaskMetadataResponse(
        format_name=task.media_format,
        duration_seconds=task.duration_seconds,
        video_codec=task.video_codec,
        width=task.width,
        height=task.height,
        frame_rate=task.frame_rate,
        audio_codec=task.audio_codec,
        sample_rate=task.sample_rate,
        channels=task.channels,
        stream_count=task.stream_count,
        bit_rate=task.media_bit_rate,
        content_hash=task.content_hash,
        probed_at=task.probed_at,
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
    settings: Settings = Depends(get_settings),
) -> TaskListResponse:
    """任务列表：关闭页面后回到页面仍可看到历史任务与当前进度。"""
    return TaskListResponse(
        items=[_to_response(task, settings) for task in list_tasks(db, limit)]
    )


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TaskResponse:
    """查询单个任务的状态、进度、失败原因与切片结果。"""
    return _to_response(_get_or_404(db, task_id), settings)


@router.post("/{task_id}/retry", response_model=TaskResponse)
def retry_task(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TaskResponse:
    """重新执行失败任务。

    只有失败的任务可以重试，避免重复处理同一产物；原始视频尚未落存储的任务不属于
    可重试范畴（应重新发起上传），因此其状态在完成上传前不会进入「失败」以外的可重试态。

    已成功上传的片段会按序号复用，重试只补齐未完成的片段。
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
    return _to_response(reset, settings)


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
