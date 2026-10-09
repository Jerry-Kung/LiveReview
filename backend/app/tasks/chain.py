"""阶段串接：全自动流程模式的唯一推进点（V0.7.0）。

对外只有 `advance(task_id, *, from_phase)`：某个阶段进入终态后调用，由它决定要不要接着跑
下一个阶段。三个调用点（切分链收尾、识别链收尾、启动补扫）见
`docs/specs/v0.7.0-design.md` §3.2。

两条硬约束，改这个文件前先读：

1. **不能在任务体内部提交下一阶段**。线程池只有 `MAX_WORKERS` 个 worker，任务体自己就占着
   一个；若在任务体里 `submit`，两个 worker 同时这么做就会双双阻塞在队列上而自锁——它们
   自己正是唯一能消费这个队列的线程。因此推进收在 `executor.run()` 的包装里：`run_sync`
   跑完、线程已归还之后才调本函数。
2. **本函数的失败一律只记日志、不抛**。串接失败与「这个阶段本身的结论」是两件事，让异常
   冒出去会污染已经成功落库的阶段状态，也会让 `executor.run` 的 finally 看起来像出了错。

**两道闸都要过**（见 spec §3.5）：任务行的 `auto_run` 快照为真，且当前开关仍为开。前者挡住
追溯——创建在手工模式的任务不会因为后来打开开关而被自动拉起；后者挡住「关掉开关后继续
花钱」——推进一个尚未发生的阶段是廉价的，而真正贵的当前阶段照常跑完（spec §3.10）。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    CLIP_STATUS_UPLOADED,
    REVIEW_STATUS_SKIPPED,
    REVIEW_STATUS_SUCCEEDED,
    UNDERSTANDING_SKIPPED,
    UNDERSTANDING_STATUS_SUCCEEDED,
    MediaClip,
    Task,
)
from app.settings_store import read_auto_pipeline
from app.tasks.runner import (
    STATUS_SUCCEEDED,
    TASK_KIND_INGEST,
    TASK_KIND_REVIEW,
    TASK_KIND_UNDERSTAND,
)

logger = logging.getLogger(__name__)

# 每个阶段的自动补跑上限。识别失败常来自一次瞬时抖动（`llm_max_attempts` 用尽、预签名地址
# 生成失败），补跑一次能救回来的比不补多得多；但只越过一次，避免模型侧持续故障时无限重试
# 地烧钱。补跑的成本是可控的：识别只重发失败的片段，复盘只重跑文本调用。
AUTO_RETRY_LIMIT = 1

# 模型未配置时的失败文案：说清「做什么能通过」，而不是只报「失败了」
MODEL_MISSING_REASON = (
    "全自动流程：模型未配置，无法启动识别。补齐 LLM_* 环境变量后，"
    "在「内容理解」页点「开始识别语音」即可继续"
)


def advance(task_id: str, *, from_phase: str) -> str | None:
    """某个阶段结束后推进全自动链路；返回这次做了什么，没有动作时返回 None。

    返回值只用于日志与用例断言，没有调用方依赖它的具体取值。
    """
    try:
        return _advance(task_id, from_phase=from_phase)
    except Exception:  # noqa: BLE001 —— 串接失败不能污染已落库的阶段结论
        logger.exception("任务 %s 的 %s 阶段串接失败，全自动链路停在此处", task_id, from_phase)
        return None


def _advance(task_id: str, *, from_phase: str) -> str | None:
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            return None
        if not task.auto_run:
            # 手工模式：各阶段由用户在界面上点按钮触发
            return None
        if not read_auto_pipeline(db):
            logger.info("任务 %s 的全自动链路因开关已关闭而停止推进", task_id)
            return None

        if from_phase == TASK_KIND_INGEST:
            return _after_ingest(db, task)
        if from_phase == TASK_KIND_UNDERSTAND:
            return _after_understanding(db, task)
        if from_phase == TASK_KIND_REVIEW:
            return _after_review(db, task)
        return None
    finally:
        db.close()


def _after_ingest(db: Session, task: Task) -> str | None:
    """切分链结束后：成功则接着识别。

    切分失败**不自动重跑**（spec §4）：它是本地算力，重跑一次是几十分钟的机器时间，而失败
    原因通常是素材本身的问题或容器缺工具，重跑不会变好。把原因留在任务上由用户判断。
    """
    if task.status != STATUS_SUCCEEDED:
        return None
    if task.video_expired or not task.object_key:
        # 视频已过保留期被清理：识别无从发起，也不需要——重传会新建一条任务
        return None
    if task.understanding_started_at is not None:
        # 这条任务已经跑过识别（例如用户手工点过一次），不重复计费
        return None
    if not _has_uploaded_clips(db, task.id):
        return None
    if not get_settings().llm_configured:
        return _stop_understanding(db, task, MODEL_MISSING_REASON)
    return _enqueue(db, task, TASK_KIND_UNDERSTAND)


def _after_understanding(db: Session, task: Task) -> str | None:
    """识别链结束后：成功则接着复盘；失败则先补跑一次，再据覆盖情况决定是否复盘。

    三种终态分开处理：

    - `succeeded`：直接串复盘。
    - `skipped`：本次没有任何可识别的输入（切片未就绪）。补跑没有意义，停。
    - `failed`：先按上限补跑一次（只重发失败的片段）；补跑结束后只要有片段成功就串复盘
      ——缺口会由 `merge_reviews` 写进分析等级与「本场不足以判断」，这比不给结论有用。
      一片都没成功则不串：复盘任务体会以 `skipped` 短路，串过去只是白跑一趟流程。
    """
    status = task.understanding_status
    if status == UNDERSTANDING_STATUS_SUCCEEDED:
        return _to_review(db, task)
    if status == UNDERSTANDING_SKIPPED:
        return None
    if _retry_left(task):
        return _retry(db, task, TASK_KIND_UNDERSTAND, "识别")
    if (task.understanding_clip_count or 0) > 0:
        return _to_review(db, task)
    logger.info("任务 %s 没有任何片段识别成功，全自动链路停在此处", task.id)
    return None


def _after_review(db: Session, task: Task) -> str | None:
    """复盘链结束后：失败则按上限补跑一次，成功或无输入都是链路的终点。"""
    if task.review_status == REVIEW_STATUS_SUCCEEDED:
        logger.info("任务 %s 的全自动链路完成", task.id)
        return "done"
    if task.review_status == REVIEW_STATUS_SKIPPED:
        return None
    if _retry_left(task):
        return _retry(db, task, TASK_KIND_REVIEW, "复盘")
    logger.info("任务 %s 复盘失败且自动补跑已用尽，全自动链路停在此处", task.id)
    return None


def _to_review(db: Session, task: Task) -> str | None:
    """串到复盘。

    前置条件与复盘入队接口的 409 判定一致：至少有一片识别成功。没有产物时不串——那会让
    库里多出一条「无输入」的复盘记录，而它对用户没有任何解释力。
    """
    if task.review_started_at is not None:
        return None
    if not _has_understood_clip(db, task.id):
        return None
    return _enqueue(db, task, TASK_KIND_REVIEW)


def _enqueue(db: Session, task: Task, phase: str) -> str:
    """把下一阶段交给执行器，并把补跑计数归零（计数描述的是「正在跑的这个阶段」）。"""
    task.auto_retries = 0
    db.commit()
    from app.tasks.executor import submit

    submit(task.id, phase)
    logger.info("任务 %s 已按全自动模式串入 %s 阶段", task.id, phase)
    return phase


def _retry(db: Session, task: Task, phase: str, label: str) -> str:
    """自动补跑一次同一阶段：计数加一后重新入队。"""
    task.auto_retries = (task.auto_retries or 0) + 1
    db.commit()
    from app.tasks.executor import submit

    submit(task.id, phase)
    logger.info(
        "任务 %s 的%s阶段未成功，按全自动模式自动补跑（当前阶段第 %d 次）",
        task.id,
        label,
        task.auto_retries,
    )
    return f"retry:{phase}"


def _stop_understanding(db: Session, task: Task, reason: str) -> str:
    """把「全自动走不下去」落成可解释的终态，而不是留一个没有进展的在途状态。"""
    task.mark_understanding_failed(reason)
    db.commit()
    logger.warning("任务 %s 的全自动链路停止：%s", task.id, reason)
    return "stopped:model_missing"


def _retry_left(task: Task) -> bool:
    return (task.auto_retries or 0) < AUTO_RETRY_LIMIT


def _has_uploaded_clips(db: Session, task_id: str) -> bool:
    """是否至少有一片已落对象存储：与识别链路「可识别片段」的判据一致。"""
    stmt = (
        select(MediaClip.id)
        .where(
            MediaClip.task_id == task_id,
            MediaClip.status == CLIP_STATUS_UPLOADED,
            MediaClip.object_key.is_not(None),
        )
        .limit(1)
    )
    return db.execute(stmt).first() is not None


def _has_understood_clip(db: Session, task_id: str) -> bool:
    """是否至少有一片识别成功：与复盘入队接口的前置条件一致。"""
    stmt = (
        select(MediaClip.id)
        .where(
            MediaClip.task_id == task_id,
            MediaClip.understanding_status == UNDERSTANDING_STATUS_SUCCEEDED,
        )
        .limit(1)
    )
    return db.execute(stmt).first() is not None


def sweep_pending() -> list[str]:
    """启动时补扫：把「上一阶段已结束、下一阶段还没入队」的全自动任务接上。

    进程在任务体提交下一阶段之前被杀，链路就断在这里——`requeue_pending` 的其它分支都只看
    「在途状态」，而这类任务的状态是干净的终态，谁都翻不到它。因此需要一次从库里反查。

    判据与 `advance` 用的是同一组事实，只是驱动方式不同：这里按落库状态找出「该被推进的
    起点阶段」，再交给 `advance` 走完整的判定链，不为补扫单写一套推进逻辑（两套迟早漂移）。

    开关关闭时直接返回空列表：关掉开关之后仍留在库里的快照任务不该在下次启动时被拉起来。
    返回本次补上的任务 id。
    """
    db = SessionLocal()
    try:
        if not read_auto_pipeline(db):
            return []
        candidates = list(
            db.execute(select(Task).where(Task.auto_run.is_(True))).scalars()
        )
        targets = [
            (task.id, phase)
            for task, phase in ((task, _pending_phase(task)) for task in candidates)
            if phase is not None
        ]
    finally:
        db.close()

    resumed: list[str] = []
    for task_id, phase in targets:
        if advance(task_id, from_phase=phase) is not None:
            resumed.append(task_id)
    if resumed:
        logger.info("启动时补上 %d 条全自动任务的后续阶段", len(resumed))
    return resumed


def _pending_phase(task: Task) -> str | None:
    """这条全自动任务当前「该被推进」的起点阶段；没有则返回 None。

    判据是「下一阶段还没启动过」（`*_started_at` 为空），而不是「下一阶段没有结论」：后者会
    把正在跑的任务也翻出来，与执行器的在途去重打架。

    **同时要求下一阶段从未结束过**（`*_finished_at` 为空）。只有「没启动」这一条会留下一个
    反复重启的空转：识别因「没有已入库的切片」而 `skipped` 时 `understanding_started_at` 仍为
    空（`_finish_task` 只写 `finished_at`），于是每次启动都会把这条任务重新入队一次，跑完又
    `skipped`。加上这条，`skipped` 的终态就等于「这个阶段已经判过了」，不会被反复翻出来。
    """
    if task.status != STATUS_SUCCEEDED:
        return None
    if task.understanding_started_at is None and task.understanding_finished_at is None:
        return TASK_KIND_INGEST
    if task.review_started_at is None and task.review_finished_at is None:
        return TASK_KIND_UNDERSTAND
    return None
