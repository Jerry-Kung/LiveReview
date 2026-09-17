"""后台任务执行器：进程内线程池 + 落库状态流转。

单实例部署下的最简方案，满足「关闭页面不影响后台任务继续执行」。多实例与进程重启后的
任务恢复不在本版范围；任务状态已落库，重启后可按 `uploaded` 状态重新入队。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from app.database import SessionLocal
from app.tasks.runner import STATUS_UPLOADED, list_tasks, reclaim_stale_processing, run_task

logger = logging.getLogger(__name__)

MAX_WORKERS = 2

_executor: ThreadPoolExecutor | None = None
_futures: dict[str, Future] = {}
# 可重入锁：submit 持锁期间会调用 _get_executor，普通 Lock 会在同一线程内自死锁
_lock = threading.RLock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="task")
    return _executor


def run_sync(task_id: str) -> None:
    """在独立会话中执行任务：线程内不复用请求会话，避免跨线程共享 Session。"""
    db = SessionLocal()
    try:
        run_task(db, task_id)
    finally:
        db.close()


def run(task_id: str) -> None:
    """线程池工作线程的入口：执行任务并清掉在途记录。"""
    try:
        run_sync(task_id)
    finally:
        with _lock:
            _futures.pop(task_id, None)


def submit(task_id: str) -> None:
    """把任务提交到线程池；同一任务重复提交只保留一次。

    提交前先确认执行器已就绪，避免在持锁期间触发懒构造。
    """
    executor = _get_executor()
    with _lock:
        existing = _futures.get(task_id)
        if existing is not None and not existing.done():
            logger.info("任务 %s 已在执行队列中，跳过重复提交", task_id)
            return
        _futures[task_id] = executor.submit(run, task_id)


def requeue_pending() -> int:
    """启动时把 `uploaded` 状态的任务重新入队，覆盖进程重启丢任务的场景。

    先把残留的 `processing` 退回 `uploaded`：进程重启后原认领凭据已失效，这类任务若不
    重置，既不会被重新入队也永远不会推进。
    """
    db = SessionLocal()
    try:
        reclaimed = reclaim_stale_processing(db)
        pending = [task.id for task in list_tasks(db, limit=100) if task.status == STATUS_UPLOADED]
    finally:
        db.close()

    if reclaimed:
        logger.info("启动时重置 %d 个中断的处理中任务", reclaimed)
    for task_id in pending:
        submit(task_id)
    if pending:
        logger.info("启动时重新入队 %d 个待处理任务", len(pending))
    return len(pending)


def shutdown() -> None:
    """等待在途任务结束；供应用关闭使用。"""
    global _executor
    with _lock:
        executor, _executor = _executor, None
        _futures.clear()
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)
