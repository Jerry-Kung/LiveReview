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


def _require_video(task: Task) -> None:
    """视频已被保留期清理的任务不能再进识别：对象已经不在桶里，发出去只会白跑一趟。

    `object_key` 为空正是「视频已清理」的判据（清理时一并清空）。过期任务的转写与复盘
    结论仍然保留可看，因此这里拒绝的是「再识别」，不是「打开这条任务」。
    """
    if not task.object_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "该任务的视频已按 72 小时保留期清理，无法识别或重试识别；"
                "转写与复盘结论仍然保留可看，如需重新识别请重新上传视频"
            ),
        )


def _require_clips(db: Session, task: Task) -> list[MediaClip]:
    """取任务的切片；没有可识别片段时给出可操作的失败原因，而不是静默跑一遍空任务。"""
    clips = list_clips(db, task.id)
    if not clips:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该任务还没有切片，请先完成切分后再启动识别",
        )
    return clips


def _submit_understanding(task_id: str, db: Session) -> None:
    """把识别交给后台执行器；执行器不可用时落成可重试的失败态，不留下假在途。

    线程池已 shutdown 时 `executor.submit` 会抛 `RuntimeError`。此处若不接住，任务会停在
    「进行中」而没有任何工作线程会推进它——过期清理的「在途任务跳过」只看状态列，那条视频
    于是永远不会被回收。落成 `failed` 才是一个既能解释、也能由「重新执行」走出去的终态。
    """
    try:
        submit(task_id, TASK_KIND_UNDERSTAND)
    except RuntimeError as exc:
        task = db.get(Task, task_id)
        if task is not None:
            task.mark_understanding_failed(f"任务未能入队：{exc}")
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="后台执行器不可用，识别未入队，请稍后重试",
        ) from exc


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
    _require_video(task)
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
    # 入队当刻即落成「进行中」：`submit()` 只把任务交给线程池，不等工作线程真正开始，
    # 因此「提交」与「任务体接管」之间必有一段间隙。若在间隙里对外报 `pending`，客户端
    # 读到的既不是「未开始」也不是「进行中」——界面看起来与没点过一模一样（V0.6.2）。
    # 状态取值与任务体共用 `Task.mark_understanding_running()`，两处不会漂移。
    task.mark_understanding_running()
    db.commit()
    _submit_understanding(task_id, db)
    logger.info("任务 %s 的识别已入队", task_id)

    response.status_code = status.HTTP_202_ACCEPTED
    return _to_response(task, settings)


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
    _require_video(task)
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
