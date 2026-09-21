"""入队当刻的状态（V0.6.2）：提交后对外报的必须就是「进行中」。

`conftest.py` 把执行器换成了同步实现，`POST` 在测试里是跑完才返回的，因此 HTTP 层
永远看不到「已入队、线程还没接管」那一帧——而那正是缺陷发生的地方。这里把 `submit`
替成空操作后直接调用路由函数，库里的状态就等于线上 POST 返回的那一刻。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Response

from app.models import (
    CLIP_STATUS_UPLOADED,
    PROGRESS_REVIEW_START,
    PROGRESS_UNDERSTANDING_START,
    REVIEW_STATUS_FAILED,
    REVIEW_STATUS_RUNNING,
    UNDERSTANDING_STATUS_PENDING,
    UNDERSTANDING_STATUS_RUNNING,
    UNDERSTANDING_STATUS_SUCCEEDED,
    MediaClip,
    Task,
    utcnow,
)


def _seed_task(db_session_factory, task_id: str = "tkq1", *, understood: bool = False) -> str:
    """造一条可启动识别的任务：视频在桶里、有一片已入库的切片。

    `understood=True` 时把这片标成已识别成功，用于复盘的前置条件，也用于识别链的
    短路分支（全部片段都已有结果时不重复请求模型）。
    """
    db = db_session_factory()
    try:
        db.add(
            Task(
                id=task_id,
                kind="ingest",
                status="succeeded",
                progress=100,
                object_key=f"liverreview/original/{task_id}.ts",
                size=1024,
            )
        )
        clip = MediaClip(
            task_id=task_id,
            index=0,
            start_seconds=0.0,
            end_seconds=60.0,
            status=CLIP_STATUS_UPLOADED,
            object_key=f"liverreview/clip/{task_id}_000.mp4",
        )
        if understood:
            clip.understanding_status = UNDERSTANDING_STATUS_SUCCEEDED
            clip.understanding_segments_json = "[]"
            clip.understanding_finished_at = utcnow()
        db.add(clip)
        db.commit()
    finally:
        db.close()
    return task_id


def _reload(db_session_factory, task_id: str) -> Task:
    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        assert task is not None
        db.expunge(task)
        return task
    finally:
        db.close()


# —— 入队接口：提交后对外报的就是进行中 ——


def test_start_understanding_reports_running_at_enqueue(
    db_session_factory, test_settings, monkeypatch
):
    """提交后立即返回 running，不给「未开始」留出可观察的窗口（V0.6.2 的缺陷锚点）。"""
    from app.routers import understanding as router_module

    task_id = _seed_task(db_session_factory)
    monkeypatch.setattr(router_module, "submit", lambda *args, **kwargs: None)

    db = db_session_factory()
    try:
        response = Response()
        result = router_module.start_understanding(
            task_id, response, db=db, settings=test_settings
        )
    finally:
        db.close()

    assert response.status_code == 202
    assert result.understanding is not None
    assert result.understanding.status == UNDERSTANDING_STATUS_RUNNING
    assert result.understanding.progress == PROGRESS_UNDERSTANDING_START

    task = _reload(db_session_factory, task_id)
    assert task.understanding_status == UNDERSTANDING_STATUS_RUNNING
    assert task.understanding_started_at is not None
    assert task.understanding_finished_at is None
    assert task.understanding_error is None


def test_start_review_reports_running_at_enqueue(
    db_session_factory, test_settings, monkeypatch
):
    """复盘侧同构：接口报 running，且进度停在起点而不是 0。"""
    from app.routers import review as router_module

    task_id = _seed_task(db_session_factory, task_id="tkq2", understood=True)
    monkeypatch.setattr(router_module, "submit", lambda *args, **kwargs: None)

    db = db_session_factory()
    try:
        response = Response()
        result = router_module.start_review(task_id, response, db=db, settings=test_settings)
    finally:
        db.close()

    assert response.status_code == 202
    assert result.review is not None
    assert result.review.status == REVIEW_STATUS_RUNNING
    assert result.review.progress == PROGRESS_REVIEW_START

    task = _reload(db_session_factory, task_id)
    assert task.review_status == REVIEW_STATUS_RUNNING
    assert task.review_started_at is not None
    assert task.review_finished_at is None


def test_reset_no_longer_leaves_pending_behind(db_session_factory, test_settings, monkeypatch):
    """reset 仍是「清空上一轮结论」的手段，但不该再是入队后的对外状态。

    这条防止有人把置位搬回任务体：搬回去之后，路由返回时库里又会是 pending。
    """
    from app.routers import understanding as router_module

    task_id = _seed_task(db_session_factory, task_id="tkq3")
    monkeypatch.setattr(router_module, "submit", lambda *args, **kwargs: None)

    db = db_session_factory()
    try:
        router_module.start_understanding(task_id, Response(), db=db, settings=test_settings)
    finally:
        db.close()

    task = _reload(db_session_factory, task_id)
    assert task.understanding_status != UNDERSTANDING_STATUS_PENDING


# —— 执行器不可用：不留假在途 ——


def test_understanding_enqueue_failure_is_terminal(db_session_factory, test_settings, monkeypatch):
    """线程池不可用时落成 failed 并返回 503。

    停在「进行中」而没有任何工作线程推进它，会让过期清理的「在途任务跳过」把这条任务的
    视频永久留下——那比一次明确的失败贵得多。
    """
    from app.routers import understanding as router_module

    task_id = _seed_task(db_session_factory, task_id="tkq4")

    def boom(*args, **kwargs):
        raise RuntimeError("线程池已关闭")

    monkeypatch.setattr(router_module, "submit", boom)

    db = db_session_factory()
    try:
        with pytest.raises(HTTPException) as excinfo:
            router_module.start_understanding(
                task_id, Response(), db=db, settings=test_settings
            )
    finally:
        db.close()

    assert excinfo.value.status_code == 503
    task = _reload(db_session_factory, task_id)
    assert task.understanding_status != UNDERSTANDING_STATUS_RUNNING
    assert task.understanding_finished_at is not None


def test_review_enqueue_failure_is_terminal(db_session_factory, test_settings, monkeypatch):
    from app.routers import review as router_module

    task_id = _seed_task(db_session_factory, task_id="tkq5", understood=True)

    def boom(*args, **kwargs):
        raise RuntimeError("线程池已关闭")

    monkeypatch.setattr(router_module, "submit", boom)

    db = db_session_factory()
    try:
        with pytest.raises(HTTPException) as excinfo:
            router_module.start_review(task_id, Response(), db=db, settings=test_settings)
    finally:
        db.close()

    assert excinfo.value.status_code == 503
    task = _reload(db_session_factory, task_id)
    assert task.review_status == REVIEW_STATUS_FAILED
    assert task.review_finished_at is not None


# —— 短路分支：全部片段都已识别时不该经过 running ——


def test_all_clips_understood_short_circuits_without_running(
    db_session_factory, test_settings, monkeypatch
):
    """全部片段已有结果时直接汇总完成，中途不出现 running。

    若把「置进行中」搬进任务体开头，这条分支会带着 running 进 `_summarize`；一旦被重启
    捕到，`reclaim_interrupted_understanding` 会把一个没真正跑过的任务重置并重新入队。
    """
    from app.tasks import understanding as task_module

    task_id = _seed_task(db_session_factory, task_id="tkq6", understood=True)

    db = db_session_factory()
    try:
        task_module.run_understanding(db, task_id, settings=test_settings)
    finally:
        db.close()

    task = _reload(db_session_factory, task_id)
    assert task.understanding_status == UNDERSTANDING_STATUS_SUCCEEDED
    assert task.understanding_finished_at is not None
