"""复盘接口（V0.3）：启动整场复盘、读取结论、下载报告。

与识别接口同构：接口只负责「确认这次请求合法」并把任务交给后台执行器，不在请求内等模型
返回——复盘虽然比逐片识别快得多，但长直播要分批调用，同步等待仍会把请求拖到超时。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import REVIEW_STATUS_RUNNING, Task
from app.schemas import TaskResponse
from app.tasks import (
    TASK_KIND_REVIEW,
    list_clips,
    load_review_result,
    reset_task_review,
    review_to_markdown,
    submit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["review"])


def _get_or_404(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return task


def _require_model(settings: Settings) -> None:
    """复盘与识别共用同一组模型配置，缺失时给出同样的可操作提示。"""
    if not settings.llm_configured:
        missing = "、".join(settings.llm_missing_fields)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模型未配置，无法启动复盘；缺少环境变量：{missing}",
        )


def _submit_review(task_id: str, db: Session) -> None:
    """把复盘交给后台执行器；执行器不可用时落成可重试的失败态，不留下假在途。

    与识别侧同构：停在「进行中」的任务没有任何工作线程会推进它，而过期清理的「在途任务
    跳过」只看状态列，那条视频于是永远不会被回收。
    """
    try:
        submit(task_id, TASK_KIND_REVIEW)
    except RuntimeError as exc:
        task = db.get(Task, task_id)
        if task is not None:
            task.mark_review_failed(f"任务未能入队：{exc}")
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="后台执行器不可用，复盘未入队，请稍后重试",
        ) from exc


@router.post(
    "/{task_id}/review",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_review(
    task_id: str,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TaskResponse:
    """启动（或重跑）整场复盘，后台执行，接口立即返回。

    前置条件是「有已识别的语音记录」：没有识别结果时给出可操作的失败原因，而不是入队一个
    注定被跳过的空任务。识别有失败片段**不阻断**复盘——缺口会写进结论的分析等级与缺口说明，
    这比让用户拿不到任何结论更有用。
    """
    from app.routers.tasks import _to_response

    task = _get_or_404(db, task_id)
    _require_model(settings)

    if task.review_status == REVIEW_STATUS_RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该任务的复盘正在进行中，请等待当前这一轮结束",
        )

    clips = list_clips(db, task_id)
    if not clips:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该任务还没有切片，请先完成切分与识别后再启动复盘",
        )
    if not any(clip.understanding_succeeded for clip in clips):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该任务还没有任何识别成功的片段，请先启动识别",
        )

    reset_task_review(db, task_id)
    # 与识别侧同样的理由：`submit()` 不等工作线程开始，间隙里对外报 `pending` 会让界面
    # 看起来与没点过一样（V0.6.2）。取值与任务体共用 `Task.mark_review_running()`。
    task.mark_review_running()
    db.commit()
    _submit_review(task_id, db)
    logger.info("任务 %s 的复盘已入队", task_id)

    response.status_code = status.HTTP_202_ACCEPTED
    return _to_response(task, settings)


@router.get("/{task_id}/review.md", response_class=Response)
def download_review(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """以 Markdown 下载复盘报告：业务验收需要把结论拿出来讨论与归档。

    与页面同源——渲染的就是落库的那份结构化结论，因此「看到的」与「下载到的」一致。
    """
    task = _get_or_404(db, task_id)
    payload = load_review_result(task) if task.review_succeeded else None
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该任务当前复盘状态为 {task.review_status}，还没有可下载的复盘结论",
        )

    content = review_to_markdown(task, payload, model_name=settings.llm_model_name)
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="review-{task_id}.md"'},
    )
