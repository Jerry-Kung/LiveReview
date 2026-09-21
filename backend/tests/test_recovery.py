"""进程中断后的自动恢复测试（关页面 / 容器重启）。

覆盖两条「用户离开页面后不该断掉」的链路：

- **上传**：分片已收齐但合并入库没跑完。上传由浏览器逐片驱动，关页面就断在这里，
  重启时必须由服务端自己接着做完。
- **识别**：停在 `running` 的任务随进程一起丢了，重启后要接着跑未完成的片段，
  且**已成功的片段不得重复请求**（那是在重复花钱）。
"""

from __future__ import annotations

import pytest

from app import chunks as chunk_store
from app.models import (
    CLIP_STATUS_UPLOADED,
    UNDERSTANDING_STATUS_PENDING,
    UNDERSTANDING_STATUS_RUNNING,
    UNDERSTANDING_STATUS_SUCCEEDED,
    MediaClip,
    Task,
    utcnow,
)
from tests.test_uploads import FILE_SIZE, _chunk_body, _create, _upload_all, _upload_one


@pytest.fixture
def resume_env(monkeypatch, db_session_factory, media_root, test_settings, storage):
    """把执行器的库、配置与存储指到测试实现，使恢复链路可以被直接调用。

    恢复是启动钩子，不经过 HTTP；这里替换的是它实际取用的几个单例。
    """
    from app.tasks import executor

    monkeypatch.setattr(executor, "SessionLocal", db_session_factory)
    monkeypatch.setattr(executor, "get_settings", lambda: test_settings)
    monkeypatch.setattr("app.uploads.get_storage", lambda: storage)
    return executor


def _session(client, upload_id: str) -> dict:
    return client.get(f"/api/uploads/{upload_id}").json()


# —— 上传中断 ——


def test_resume_finishes_upload_whose_chunks_are_all_on_disk(
    client, resume_env, storage, media_root, db_session_factory
):
    """分片齐了但没提交 complete（关页面）→ 启动恢复把合并入库接着做完。"""
    data = _create(client)
    upload_id, task_id = data["upload_id"], data["task_id"]
    _upload_all(client, upload_id)

    # 模拟「浏览器传完最后一片就关了页面」：没有 complete 请求
    assert _session(client, upload_id)["status"] == "uploading"
    submitted: list[tuple[str, str]] = []
    resume_env.submit = lambda tid, phase="ingest": submitted.append((tid, phase))  # type: ignore[assignment]

    assert resume_env.resume_interrupted_uploads() == 1

    session = _session(client, upload_id)
    assert session["status"] == "completed"
    assert session["error"] is None
    # 分片与合并临时文件都已清理，只剩对象存储里的那份原始视频
    assert chunk_store.received_indices(media_root, upload_id) == []
    assert not chunk_store.merged_path(media_root, upload_id).exists()
    assert len(storage.uploaded_keys) == 1
    # 任务已交给后台执行器，而不是只改状态不跑
    assert submitted == [(task_id, "ingest")]


def test_resume_leaves_incomplete_upload_alone(client, resume_env, media_root):
    """缺片的会话保持原样：剩下几片只能由浏览器补传，恢复不了也不该被判失败。"""
    data = _create(client)
    upload_id = data["upload_id"]
    assert client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0)).status_code == 204

    assert resume_env.resume_interrupted_uploads() == 0

    session = _session(client, upload_id)
    assert session["status"] == "uploading"
    assert session["error"] is None
    assert chunk_store.received_indices(media_root, upload_id) == [0]


def test_resume_is_idempotent_after_completion(client, resume_env, storage):
    """已完成的会话不会被再跑一遍：重复执行不该在桶里留下第二份原视频。"""
    _upload_one(client, storage)
    originals = _original_keys(storage)
    assert len(originals) == 1

    assert resume_env.resume_interrupted_uploads() == 0
    assert _original_keys(storage) == originals


def _original_keys(storage) -> list[str]:
    """只为原始视频取键：切分链路随后会往同一个桶里上传各切片，这里不关心它们。"""
    return [key for key in storage.uploaded_keys if "/original/" in key]


def test_resume_reuses_local_copy_when_object_is_missing(
    client, resume_env, storage, media_root, db_session_factory, test_settings
):
    """上一轮已合并成本地副本但入库失败：恢复直接上传那份副本，不必再从分片拼一次。"""
    from app.media.paths import original_path

    data = _create(client)
    upload_id, task_id = data["upload_id"], data["task_id"]
    _upload_all(client, upload_id)

    copy = original_path(media_root, task_id, "live.ts")
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"x" * FILE_SIZE)

    resume_env.submit = lambda *args, **kwargs: None  # type: ignore[assignment]
    assert resume_env.resume_interrupted_uploads() == 1

    assert len(storage.uploaded_keys) == 1
    assert storage.get_object_bytes(storage.uploaded_keys[0]) == b"x" * FILE_SIZE
    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["object_key"] == storage.uploaded_keys[0]
    # 副本在上传成功后即释放（V0.6.1）：任务体需要时按对象键重新取回
    assert not copy.exists()


def test_resume_reports_failure_without_losing_chunks(
    client, resume_env, storage, media_root, db_session_factory
):
    """恢复时入库失败：原因落库供用户重试，分片保留以免重传整个原文件。"""
    from app.storage.mock import FaultMode

    data = _create(client)
    upload_id = data["upload_id"]
    _upload_all(client, upload_id)
    storage.set_fault_mode(FaultMode.SERVER)

    assert resume_env.resume_interrupted_uploads() == 0

    session = _session(client, upload_id)
    assert session["status"] == "failed"
    assert session["error"]
    assert chunk_store.received_indices(media_root, upload_id) == [0, 1, 2]


def test_requeue_pending_resumes_uploads_and_understanding_in_one_pass(
    client, resume_env, storage, media_root, db_session_factory, monkeypatch
):
    """一次启动把三类在途工作都捡回来：未入库的上传、处理中的任务、被打断的识别。"""
    from app.tasks import TASK_KIND_UNDERSTAND, requeue_pending

    data = _create(client)
    upload_id, task_id = data["upload_id"], data["task_id"]
    _upload_all(client, upload_id)
    understanding_task = _seed_interrupted_understanding(db_session_factory, clip_succeeded=True)

    submitted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        resume_env, "submit", lambda tid, phase="ingest": submitted.append((tid, phase))
    )

    resumed = requeue_pending()
    assert resumed >= 2
    # 上传恢复走完整入库链路，并把这个任务交给切分阶段
    assert (task_id, "ingest") in submitted
    assert _session(client, upload_id)["status"] == "completed"
    # 识别以独立的阶段入队，不会与切分阶段互相顶掉
    assert (understanding_task, TASK_KIND_UNDERSTAND) in submitted


# —— 识别中断 ——


def _seed_interrupted_understanding(db_session_factory, *, clip_succeeded: bool) -> str:
    """造一条「识别跑到一半进程没了」的任务：任务级 running，片段一成功一在途。"""
    db = db_session_factory()
    try:
        db.add(
            Task(
                id="tk1",
                kind="ingest",
                status="succeeded",
                progress=100,
                understanding_status=UNDERSTANDING_STATUS_RUNNING,
                understanding_progress=50,
                understanding_started_at=utcnow(),
            )
        )
        db.add(
            MediaClip(
                task_id="tk1",
                index=0,
                start_seconds=0.0,
                end_seconds=60.0,
                status=CLIP_STATUS_UPLOADED,
                object_key="liverreview/clip/clip_000_1_aaaa.mp4",
                understanding_status=(
                    UNDERSTANDING_STATUS_SUCCEEDED if clip_succeeded else UNDERSTANDING_STATUS_RUNNING
                ),
                understanding_segments_json="[]" if clip_succeeded else None,
                understanding_finished_at=utcnow() if clip_succeeded else None,
            )
        )
        db.add(
            MediaClip(
                task_id="tk1",
                index=1,
                start_seconds=60.0,
                end_seconds=120.0,
                status=CLIP_STATUS_UPLOADED,
                object_key="liverreview/clip/clip_001_1_bbbb.mp4",
                understanding_status=UNDERSTANDING_STATUS_RUNNING,
            )
        )
        db.commit()
    finally:
        db.close()
    return "tk1"


def test_interrupted_understanding_is_reset_and_requeued(client, resume_env, db_session_factory):
    """停在 running 的识别必须退回可续跑状态，否则这场识别永远卡在「识别中」。"""
    from app.tasks import reclaim_interrupted_understanding

    task_id = _seed_interrupted_understanding(db_session_factory, clip_succeeded=False)

    db = db_session_factory()
    try:
        assert reclaim_interrupted_understanding(db) == [task_id]
        task = db.get(Task, task_id)
        assert task.understanding_status == UNDERSTANDING_STATUS_PENDING
        assert task.understanding_progress == 0
        assert task.understanding_finished_at is None
        # 在途片段退回未识别；退化到 pending 才能被续跑重新认领
        assert [clip.understanding_status for clip in task.clips] == [
            UNDERSTANDING_STATUS_PENDING,
            UNDERSTANDING_STATUS_PENDING,
        ]
    finally:
        db.close()


def test_completed_clip_is_kept_when_understanding_resumes(client, resume_env, db_session_factory):
    """已成功的片段在恢复时不被动过：续跑只补未完成的部分，不重复花钱。"""
    from app.tasks import reclaim_interrupted_understanding

    _seed_interrupted_understanding(db_session_factory, clip_succeeded=True)

    db = db_session_factory()
    try:
        reclaim_interrupted_understanding(db)
        first = db.get(Task, "tk1").clips[0]
        assert first.understanding_status == UNDERSTANDING_STATUS_SUCCEEDED
        # 成功的片段仍带完成时间，界面上不会被重新渲染成「未识别」
        assert first.understanding_finished_at is not None
    finally:
        db.close()


def test_requeue_resumes_interrupted_understanding(
    client, resume_env, db_session_factory, monkeypatch
):
    """启动恢复的入口把被打断的识别重新入队，而不是只重置状态。"""
    from app.tasks import TASK_KIND_UNDERSTAND, requeue_pending

    task_id = _seed_interrupted_understanding(db_session_factory, clip_succeeded=True)

    submitted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        resume_env, "submit", lambda tid, phase="ingest": submitted.append((tid, phase))
    )

    assert requeue_pending() >= 1
    assert (task_id, TASK_KIND_UNDERSTAND) in submitted


def test_understanding_never_started_is_not_resumed(client, resume_env, db_session_factory, monkeypatch):
    """从未启动过识别的任务不会被恢复翻出来：识别要花钱，必须由用户明确点一次。"""
    from app.tasks import requeue_pending

    db = db_session_factory()
    try:
        db.add(Task(id="tk2", kind="ingest", status="succeeded", progress=100))
        db.add(
            MediaClip(
                task_id="tk2",
                index=0,
                start_seconds=0.0,
                end_seconds=60.0,
                status=CLIP_STATUS_UPLOADED,
                object_key="liverreview/clip/clip_000_2_cccc.mp4",
            )
        )
        db.commit()
    finally:
        db.close()

    submitted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        resume_env, "submit", lambda tid, phase="ingest": submitted.append((tid, phase))
    )

    requeue_pending()
    assert all(tid != "tk2" for tid, _ in submitted)
