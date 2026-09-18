"""识别接口（V0.2）：启动整场识别、重试单个片段、读取语音记录汇总。

接口只负责「确认这次请求合法」并交给后台执行器，不在请求内等待模型返回：一场直播的
识别是分钟到小时级任务，同步等待会让客户端超时、也无法在关页面后继续。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import (
    UNDERSTANDING_STATUS_RUNNING,
    MediaClip,
    Task,
)
from app.schemas import (
    TaskResponse,
    TranscriptClipResponse,
    TranscriptResponse,
    TranscriptSegmentResponse,
)
from app.tasks import (
    TASK_KIND_UNDERSTAND,
    list_clips,
    reset_task_understanding,
    retry_clip_understanding,
    submit,
)
from app.transcript import build_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["understanding"])


def _get_or_404(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return task


def _require_clips(db: Session, task: Task) -> list[MediaClip]:
    """取任务的切片；没有可识别片段时给出可操作的失败原因，而不是静默跑一遍空任务。"""
    clips = list_clips(db, task.id)
    if not clips:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该任务还没有切片，请先完成切分后再启动识别",
        )
    return clips


@router.post(
    "/{task_id}/understanding",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_understanding(
    task_id: str,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TaskResponse:
    """启动（或续跑）整场识别，后台执行，接口立即返回。

    续跑语义：已识别成功的片段不重复请求，只为未完成与失败的片段发新请求。这既是重试
    失败的补课路径，也避免重复点击把同一批片段重新计费一遍。
    """
    from app.routers.tasks import _to_response

    task = _get_or_404(db, task_id)
    _require_clips(db, task)

    if task.understanding_status == UNDERSTANDING_STATUS_RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该任务的识别正在进行中，请等待当前这一轮结束",
        )

    if not settings.llm_configured:
        missing = "、".join(settings.llm_missing_fields)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模型未配置，无法启动识别；缺少环境变量：{missing}",
        )

    reset_task_understanding(db, task_id)
    submit(task_id, TASK_KIND_UNDERSTAND)
    logger.info("任务 %s 的识别已入队", task_id)

    response.status_code = status.HTTP_202_ACCEPTED
    return _to_response(_get_or_404(db, task_id), settings)


@router.post("/{task_id}/clips/{index}/understanding", response_model=TaskResponse)
def retry_clip(
    task_id: str,
    index: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TaskResponse:
    """同步重试单个片段的识别。

    单片识别通常几十秒到几分钟，同步等待可以让用户立刻看到这一片的结果，且失败原因
    直接作为接口错误返回；整场识别仍然走后台。
    """
    from app.routers.tasks import _to_response

    task = _get_or_404(db, task_id)
    if not settings.llm_configured:
        missing = "、".join(settings.llm_missing_fields)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模型未配置，无法启动识别；缺少环境变量：{missing}",
        )

    clip = retry_clip_understanding(db, task_id, index, settings=settings)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"片段 #{index} 不存在或尚未入库，无法识别",
        )

    if clip.understanding_status == "failed":
        # 失败原因已经落在片段上并随响应返回，这里只记录一条便于排查的日志
        logger.warning("片段 %d 重试识别仍失败：%s", index, clip.understanding_error)

    return _to_response(_get_or_404(db, task_id), settings)


@router.get("/{task_id}/transcript.txt", response_class=Response)
def download_transcript(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """以 `text/plain` 下载全文：验收时需要把识别结果拿出来人工核对。"""
    task = _get_or_404(db, task_id)
    summary = build_summary(
        task,
        list_clips(db, task_id),
        model_name=settings.llm_model_name,
    )
    filename = f"transcript-{task_id}.txt"
    return Response(
        content=summary.text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{task_id}/transcript", response_model=TranscriptResponse)
def get_transcript(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TranscriptResponse:
    """整场语音记录汇总：片段状态、按时间排序的条目与可直接复制的全文。"""
    task = _get_or_404(db, task_id)
    summary = build_summary(
        task,
        list_clips(db, task_id),
        model_name=settings.llm_model_name,
    )

    return TranscriptResponse(
        task_id=summary.task_id,
        filename=summary.filename,
        status=summary.status,
        clip_count=summary.clip_count,
        succeeded_clip_count=summary.succeeded_clip_count,
        failed_clip_count=summary.failed_clip_count,
        segment_count=summary.segment_count,
        model_name=summary.model_name,
        clips=[
            TranscriptClipResponse(
                index=block.index,
                start_seconds=block.start_seconds,
                end_seconds=block.end_seconds,
                status=block.status,
                segment_count=block.segment_count,
                error=block.error,
                warnings=block.warnings,
            )
            for block in summary.clips
        ],
        segments=[
            TranscriptSegmentResponse(
                index=position,
                clip_index=int(segment.get("clip_index") or 0),
                start_seconds=float(segment.get("start_seconds") or 0.0),
                end_seconds=float(segment.get("end_seconds") or 0.0),
                duration_seconds=float(segment.get("end_seconds") or 0.0)
                - float(segment.get("start_seconds") or 0.0),
                content=str(segment.get("content") or ""),
                tone=str(segment.get("tone") or ""),
                clip_start_seconds=float(segment.get("clip_start_seconds") or 0.0),
                clip_end_seconds=float(segment.get("clip_end_seconds") or 0.0),
                out_of_range=bool(segment.get("out_of_range")),
            )
            for position, segment in enumerate(summary.segments)
        ],
        text=summary.text,
    )
