"""后台任务执行器：进程内线程池 + 落库状态流转。

单实例部署下的最简方案，满足「关闭页面不影响后台任务继续执行」。多实例与横向扩展不在本版
范围；进程重启丢掉的在途工作由启动时的 `requeue_pending` 恢复（见该函数的说明）。

两条任务体共用同一个线程池，**不拆池**：它们在业务上串联（切分完成才有片段可识别），
拆池会让「切片还在跑、识别已占满另一池」这种本可避免的并发白白吃掉配额。
识别耗时长但线程全程阻塞在 HTTP 上，与切分争抢的主要是连接数而非 CPU。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from sqlalchemy import select

from app import chunks as chunk_store
from app.config import get_settings
from app.database import SessionLocal
from app.models import Task, UploadSession
from app.tasks.runner import (
    STATUS_UPLOADED,
    TASK_KIND_INGEST,
    TASK_KIND_REVIEW,
    TASK_KIND_UNDERSTAND,
    list_tasks,
    reclaim_interrupted_review,
    reclaim_interrupted_understanding,
    reclaim_stale_processing,
    run_task,
)
from app.tasks.review import run_review
from app.tasks.understanding import run_understanding

logger = logging.getLogger(__name__)

MAX_WORKERS = 2

_executor: ThreadPoolExecutor | None = None
_futures: dict[tuple[str, str], Future] = {}
# 可重入锁：submit 持锁期间会调用 _get_executor，普通 Lock 会在同一线程内自死锁
_lock = threading.RLock()

# 视频过期清理线程（V0.6.1）：与任务线程池分开——它是周期性维护，不参与任务排队，
# 也不该占用任务池的并发额度。放在执行器里的理由是两者同属「进程内后台工作」，
# 启动与关闭的时序由同一处管理。
_cleanup_thread: threading.Thread | None = None
_cleanup_stop = threading.Event()


def start_cleanup_loop(*, interval_seconds: int | None = None) -> None:
    """启动视频过期清理线程；已在运行时为无操作。

    线程是 daemon：进程被强杀时不必等它，残留的未清理对象由下一次启动接着处理。
    每一轮都单独捕获异常——一轮失败（网络抖动、数据库忙）不该让清理永久停摆。
    """
    global _cleanup_thread
    settings = get_settings()
    if not settings.cleanup_enabled:
        logger.info("视频过期清理已关闭（CLEANUP_ENABLED=false）")
        return

    interval = settings.cleanup_interval_seconds if interval_seconds is None else interval_seconds
    with _lock:
        if _cleanup_thread is not None and _cleanup_thread.is_alive():
            return
        _cleanup_stop.clear()
        _cleanup_thread = threading.Thread(
            target=_cleanup_loop,
            args=(interval,),
            name="cleanup",
            daemon=True,
        )
        _cleanup_thread.start()
    logger.info("视频过期清理已启动：每 %d 秒扫描一次", interval)


def stop_cleanup_loop() -> None:
    """停止清理线程并等待其退出；供应用关闭使用。"""
    global _cleanup_thread
    with _lock:
        thread, _cleanup_thread = _cleanup_thread, None
    if thread is None:
        return
    _cleanup_stop.set()
    thread.join(timeout=5)
    # join 超时不强杀：daemon 线程会随进程退出，这里只记一条便于排查
    if thread.is_alive():
        logger.warning("视频过期清理线程未在超时内退出，随进程退出")


def _cleanup_loop(interval: int) -> None:
    """清理线程主循环：按周期扫描过期视频，直到收到停止信号。"""
    from app.tasks.cleanup import sweep_expired_videos

    while not _cleanup_stop.wait(interval):
        try:
            removed = sweep_expired_videos()
            if removed:
                logger.info("本轮清理了 %d 条任务的过期视频", removed)
        except Exception:  # noqa: BLE001 —— 单轮失败不终止线程
            logger.exception("视频过期清理失败，下一轮继续")


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="task")
    return _executor


def list_unfinished_uploads(db) -> list[str]:
    """尚未入库的上传会话 id（按创建时间升序）。

    `completed` 之外的三种状态都可能停在「分片齐了但没人合并」：关页面中断的会话停在
    `uploading`，合并途中被杀停在 `assembling`，入库失败停在 `failed`。是否真的能续跑由
    `_resume_one` 按磁盘上的分片判断，这里不做过滤。
    """
    from app.uploads import STATUS_COMPLETED

    return list(
        db.execute(
            select(UploadSession.id)
            .where(UploadSession.status != STATUS_COMPLETED)
            .order_by(UploadSession.created_at)
        ).scalars()
    )


def run_sync(task_id: str, phase: str) -> None:
    """在独立会话中执行任务体：线程内不复用请求会话，避免跨线程共享 Session。"""
    db = SessionLocal()
    try:
        if phase == TASK_KIND_UNDERSTAND:
            run_understanding(db, task_id)
        elif phase == TASK_KIND_REVIEW:
            run_review(db, task_id)
        else:
            run_task(db, task_id)
    finally:
        db.close()


def run(task_id: str, phase: str) -> None:
    """线程池工作线程的入口：执行任务体并清掉在途记录。"""
    try:
        run_sync(task_id, phase)
    finally:
        with _lock:
            _futures.pop((phase, task_id), None)


def submit(task_id: str, phase: str = TASK_KIND_INGEST) -> None:
    """把任务提交到线程池；同一任务的同一阶段重复提交只保留一次。

    识别与复盘都是独立阶段，与切分阶段的在途记录分开跟踪：同一任务的重复点击不会并发出
    两次模型请求，即使用户连点也只跑一轮。

    提交前先确认执行器已就绪，避免在持锁期间触发懒构造。
    """
    executor = _get_executor()
    key = (phase, task_id)
    with _lock:
        existing = _futures.get(key)
        if existing is not None and not existing.done():
            logger.info("任务 %s 的 %s 阶段已在执行队列中，跳过重复提交", task_id, phase)
            return
        _futures[key] = executor.submit(run, task_id, phase)


def requeue_pending() -> int:
    """启动时恢复被中断的工作，返回重新入队的处理任务数。

    覆盖三类「进程重启丢掉在途工作」的场景，都是启动时一次性扫描：

    1. **分片已收齐但尚未入库的上传**：上传由浏览器逐片驱动，关页面就断在「分片在盘上、
       没人合并入库」。这里把这类会话交给 `app/uploads.py` 的 `finalize` 接着做完，用户
       不必回来重新上传；分片没收齐的会话保持原状，仍可续传。
    2. **残留的 `processing` 任务**：退回 `uploaded` 并重新入队（认领凭据已失效）。
    3. **被打断的识别**：退回 `pending` 并重新入队（见 `reclaim_interrupted_understanding`）。
    4. **被打断的复盘**：同上，见 `reclaim_interrupted_review`——复盘重跑只花一次文本调用的
       钱，且它的输入（识别结果）已经落库，重跑不会连带重跑识别。

    识别与复盘的恢复都只在「曾经启动过」的任务上发生，且已成功的片段不会重复请求，因此
    重启不会凭空产生模型费用。
    """
    recovered = resume_interrupted_uploads()

    db = SessionLocal()
    try:
        reclaimed = reclaim_stale_processing(db)
        resumed_understanding = reclaim_interrupted_understanding(db)
        resumed_review = reclaim_interrupted_review(db)
        pending = [task.id for task in list_tasks(db, limit=100) if task.status == STATUS_UPLOADED]
    finally:
        db.close()

    if reclaimed:
        logger.info("启动时重置 %d 个中断的处理中任务", reclaimed)
    for task_id in pending:
        submit(task_id)
    if pending:
        logger.info("启动时重新入队 %d 个待处理任务", len(pending))

    for task_id in resumed_understanding:
        submit(task_id, TASK_KIND_UNDERSTAND)
    if resumed_understanding:
        logger.info("启动时续跑 %d 个中断的识别任务", len(resumed_understanding))
        if not get_settings().llm_configured:
            # 不拦：配置补齐后重启即会续跑。但要留下线索，否则用户只会看到识别「没动静」
            logger.warning(
                "识别已退回待续跑，但模型未配置，本轮会直接失败；补齐 LLM_* 配置后重启即可继续"
            )

    for task_id in resumed_review:
        submit(task_id, TASK_KIND_REVIEW)
    if resumed_review:
        logger.info("启动时续跑 %d 个中断的复盘任务", len(resumed_review))

    return len(pending) + len(resumed_understanding) + len(resumed_review) + recovered


def resume_interrupted_uploads() -> int:
    """把分片已收齐但未入库的上传接着做完，返回恢复的会话数。

    只认「分片齐了」的会话：缺片的会话仍然需要浏览器把剩下的分片传上来，恢复不了，也不该
    在这里把它判失败（用户回来续传仍然有效）。
    """
    settings = get_settings()

    db = SessionLocal()
    try:
        candidates = list_unfinished_uploads(db)
    except Exception:  # noqa: BLE001 —— 恢复失败不能让服务起不来
        logger.exception("扫描未完成的上传会话失败")
        return 0
    finally:
        db.close()

    resumed = 0
    for upload_id in candidates:
        try:
            if _resume_one(upload_id, settings):
                resumed += 1
        except Exception:  # noqa: BLE001 —— 单个会话失败不影响其余会话
            logger.exception("恢复上传会话 %s 失败", upload_id)
    return resumed


def _resume_one(upload_id: str, settings) -> bool:
    """尝试恢复单个上传会话；分片不齐或状态已推进时返回 False（保持原状）。

    每次调用开一个自己的会话期：合并 GB 级文件耗时可观，不能让一个事务把所有会话串在一起。
    """
    from app import uploads as pipeline

    task_id: str | None = None
    db = SessionLocal()
    try:
        session = db.get(UploadSession, upload_id)
        if session is None or session.status == pipeline.STATUS_COMPLETED:
            return False
        task = db.query(Task).filter(Task.upload_id == session.id).one_or_none()
        if task is None:
            return False
        task_id = task.id

        total = chunk_store.chunk_count(session.declared_size, session.chunk_size)
        if chunk_store.missing_indices(settings.media_root, session.id, total):
            # 缺片：留待用户回来补齐，这不是失败
            return False

        logger.info("恢复未完成的上传会话 %s：%s", session.id, session.filename)
        pipeline.finalize(db, session, task, settings)
    except pipeline.UploadFinalizeError as exc:
        # 失败原因已落库，任务可从界面重试
        logger.warning("会话 %s 恢复失败：%s", upload_id, exc)
        return False
    finally:
        db.close()

    if task_id is not None:
        submit(task_id)
    return True


def shutdown() -> None:
    """等待在途任务结束；供应用关闭使用。"""
    global _executor
    with _lock:
        executor, _executor = _executor, None
        _futures.clear()
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)
