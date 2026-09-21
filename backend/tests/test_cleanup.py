"""项目数据清理的测试（V0.6.1）：本地不留视频、云端 72 小时过期。

覆盖两条链路：

- **本地**：任务体无论成败都释放原始副本与转封装产物；上传入库成功后释放副本；启动时
  清掉已无主的残留文件。
- **云端**：超过保留期的原始视频与切片被删除，且**只删视频、不删结论**——转写与复盘
  结论早已落库，过期后仍要能查看。

在途任务（处理中 / 识别中 / 复盘中）必须跳过：任务体正在读那个对象，删了就会留下半个
世界的状态。删除失败要保留 `object_key`，否则会留下永远无人回收的孤儿对象。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.media.paths import clips_dir, converted_path, original_path
from app.models import (
    CLIP_STATUS_UPLOADED,
    REVIEW_STATUS_RUNNING,
    UNDERSTANDING_STATUS_RUNNING,
    MediaClip,
    Task,
    utcnow,
)
from app.storage.mock import FaultMode
from tests.conftest import FILE_SIZE
from tests.test_uploads import _create, _upload_all


def _complete(client: TestClient, filename: str = "live.ts") -> dict:
    data = _create(client, filename=filename)
    _upload_all(client, data["upload_id"])
    resp = client.post(f"/api/uploads/{data['upload_id']}/complete")
    assert resp.status_code == 200, resp.text
    return data


def _task(client: TestClient, task_id: str) -> dict:
    resp = client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def cleanup_env(monkeypatch, db_session_factory, test_settings, storage):
    """把清理模块的库、配置与存储指向测试实现。

    清理是后台动作，不经过 HTTP 依赖注入，因此替换的是它实际取用的那几个单例。
    """
    from app.tasks import cleanup

    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)
    monkeypatch.setattr(cleanup, "get_settings", lambda: test_settings)
    monkeypatch.setattr(cleanup, "get_storage", lambda: storage)
    return cleanup


# —— 本地：任务体释放 ——


def test_no_local_products_remain_after_successful_processing(client, media_root):
    """处理成功后本地产物全部释放，磁盘上不留任何视频文件。"""
    data = _complete(client)
    task_id = data["task_id"]

    assert _task(client, task_id)["status"] == "succeeded"
    assert not original_path(media_root, task_id, "live.ts").exists()
    assert not converted_path(media_root, task_id).exists()
    assert not clips_dir(media_root, task_id).exists()


def test_local_copy_is_released_after_upload_finalize(client, media_root):
    """入库成功后本地副本即释放：探测与切分改为按需从对象存储取回。

    这条是 V0.6.1 的核心改动——此前副本会一直留到任务体跑完（失败时甚至长期保留）。
    """
    data = _complete(client)

    assert not original_path(media_root, data["task_id"], "live.ts").exists()
    # 体积信息仍然正确：它必须在删副本之前取好
    assert _task(client, data["task_id"])["size"] == FILE_SIZE


def test_processing_redownloads_source_when_local_copy_is_missing(
    client, storage, media_root, db_session_factory
):
    """副本缺失时任务体从对象存储取回：释放本地文件不能把重试路径弄断。"""
    from app.media.paths import original_path as _original_path
    from app.tasks import submit

    data = _complete(client)
    task_id = data["task_id"]
    assert _task(client, task_id)["status"] == "succeeded"

    # 回到「已入库待处理」，并明确确认本地没有副本
    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.status = "uploaded"
        task.process_token = None
        task.progress = 0
        db.commit()
        object_key = task.object_key
    finally:
        db.close()
    assert object_key is not None
    assert not _original_path(media_root, task_id, "live.ts").exists()

    submit(task_id)
    assert _task(client, task_id)["status"] == "succeeded"
    # 重跑又从桶里取了一份，跑完再次释放
    assert not _original_path(media_root, task_id, "live.ts").exists()


# —— 本地：无主残留清扫 ——


def test_sweep_removes_orphans_and_keeps_owned_files(
    client, cleanup_env, media_root, db_session_factory
):
    """只清无主残留：有主的副本必须原地不动，误删会让用户重传整个原文件。"""
    data = _complete(client)
    owned_id = data["task_id"]

    from app.media.paths import originals_dir

    root = originals_dir(media_root)
    root.mkdir(parents=True, exist_ok=True)
    # 有主：任务仍在库里，不能删
    owned = root / f"{owned_id}.ts"
    owned.write_bytes(b"owned")
    # 无主：库里的任务已不存在
    orphan = root / "deadbeefdeadbeef.ts"
    orphan.write_bytes(b"orphan")
    # 无主的切片目录与分片目录同样要回收
    orphan_clips = clips_dir(media_root, "deadbeefdeadbeef")
    orphan_clips.mkdir(parents=True, exist_ok=True)
    (orphan_clips / "clip_000.mp4").write_bytes(b"x")

    removed = cleanup_env.sweep_orphan_media(media_root)

    assert removed >= 2
    assert orphan.exists() is False
    assert not orphan_clips.exists()
    assert owned.is_file()


# —— 云端：过期清理 ——


def _seed_task(
    db_session_factory,
    *,
    task_id: str,
    age_hours: float,
    status: str = "succeeded",
    understanding_status: str = "succeeded",
    review_status: str = "pending",
    with_clip: bool = True,
) -> str:
    """造一条视频仍在桶里的任务，`created_at` 往前推以控制是否到期。"""
    created = utcnow() - timedelta(hours=age_hours)
    db = db_session_factory()
    try:
        db.add(
            Task(
                id=task_id,
                kind="ingest",
                status=status,
                progress=100,
                object_key=f"liverreview/original/{task_id}.ts",
                size=1234,
                understanding_status=understanding_status,
                review_status=review_status,
                created_at=created,
            )
        )
        if with_clip:
            db.add(
                MediaClip(
                    task_id=task_id,
                    index=0,
                    start_seconds=0.0,
                    end_seconds=60.0,
                    status=CLIP_STATUS_UPLOADED,
                    object_key=f"liverreview/clip/{task_id}_000.mp4",
                )
            )
        db.commit()
    finally:
        db.close()
    return task_id


def _put_object(storage, tmp_path, object_key: str, payload: bytes = b"payload") -> str:
    """把一份内容放进（Mock）桶里，便于断言删除确实发生。

    Mock 的契约与真实后端一致：只接受 `upload_file(本地路径, 对象键)`，没有直接写字节的
    接口，因此这里先落一个临时文件再上传。
    """
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    storage.upload_file(source, object_key)
    return object_key


def test_sweep_leaves_unexpired_video_alone(client, cleanup_env, db_session_factory, storage):
    """未到期的任务不动：保留期内视频必须可正常取用。"""
    _seed_task(db_session_factory, task_id="fresh1", age_hours=1)

    assert cleanup_env.sweep_expired_videos() == 0

    db = db_session_factory()
    try:
        task = db.get(Task, "fresh1")
        assert task.object_key is not None
        assert task.video_expired_at is None
    finally:
        db.close()


def test_sweep_clears_expired_video_and_keeps_conclusions(
    client, cleanup_env, db_session_factory, storage, tmp_path
):
    """到期即清视频，但结论一字不动：转写与复盘结论都已落库，与视频是否还在无关。"""
    _seed_task(db_session_factory, task_id="old1", age_hours=100)
    _put_object(storage, tmp_path, "liverreview/original/old1.ts", b"video")
    _put_object(storage, tmp_path, "liverreview/clip/old1_000.mp4", b"clip")

    assert cleanup_env.sweep_expired_videos() == 1

    db = db_session_factory()
    try:
        task = db.get(Task, "old1")
        assert task.object_key is None
        assert task.video_expired_at is not None
        assert "保留期" in (task.error or "")
        # 结论类字段保持原样
        assert task.understanding_status == "succeeded"
    finally:
        db.close()
    # 片段行的对象键保留（记录历史事实），但桶里的对象已经删掉
    assert storage.object_count == 0


def test_sweep_skips_in_flight_tasks(client, cleanup_env, db_session_factory):
    """在途任务一律跳过：任务体正在读那个对象，删了只会留下半个世界的状态。"""
    _seed_task(db_session_factory, task_id="busy1", age_hours=100, status="processing")
    _seed_task(
        db_session_factory,
        task_id="busy2",
        age_hours=100,
        understanding_status=UNDERSTANDING_STATUS_RUNNING,
    )
    _seed_task(
        db_session_factory,
        task_id="busy3",
        age_hours=100,
        review_status=REVIEW_STATUS_RUNNING,
    )

    assert cleanup_env.sweep_expired_videos() == 0

    db = db_session_factory()
    try:
        for task_id in ("busy1", "busy2", "busy3"):
            task = db.get(Task, task_id)
            assert task.object_key is not None, task_id
            assert task.video_expired_at is None, task_id
    finally:
        db.close()


def test_sweep_keeps_object_key_when_delete_fails(
    client, cleanup_env, db_session_factory, storage, tmp_path
):
    """删除失败必须保留对象键与未标记状态：否则留下永远无人回收的孤儿对象。"""
    _seed_task(db_session_factory, task_id="fault1", age_hours=100)
    _put_object(storage, tmp_path, "liverreview/original/fault1.ts", b"video")
    storage.set_fault_mode(FaultMode.SERVER)

    assert cleanup_env.sweep_expired_videos() == 0

    db = db_session_factory()
    try:
        task = db.get(Task, "fault1")
        assert task.object_key is not None
        assert task.video_expired_at is None
    finally:
        db.close()

    # 故障恢复后下一轮能清干净
    storage.set_fault_mode(FaultMode.NONE)
    assert cleanup_env.sweep_expired_videos() == 1


def test_expired_task_refuses_understanding_but_keeps_transcript(
    client, cleanup_env, db_session_factory, storage, tmp_path
):
    """过期后不能再识别（对象已不在），但转写接口照常可读：结论要留得下来。"""
    data = _complete(client)
    task_id = data["task_id"]
    _put_object(storage, tmp_path, "liverreview/original/x.ts", b"v")

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.created_at = utcnow() - timedelta(hours=100)
        db.commit()
    finally:
        db.close()

    assert cleanup_env.sweep_expired_videos() == 1

    assert client.get(f"/api/tasks/{task_id}").json()["video_expired"] is True
    resp = client.post(f"/api/tasks/{task_id}/understanding")
    assert resp.status_code == 409
    assert "保留期" in resp.json()["detail"]
    assert client.get(f"/api/tasks/{task_id}/transcript").status_code == 200

    retry = client.post(f"/api/tasks/{task_id}/retry")
    assert retry.status_code == 409
    assert "重新上传" in retry.json()["detail"]


def test_cleanup_loop_can_be_disabled(monkeypatch, test_settings):
    """`CLEANUP_ENABLED=false` 时不启动清理线程：测试与本地开发不该被后台扫描打扰。"""
    from app.tasks import executor

    disabled = test_settings.model_copy(update={"cleanup_enabled": False})
    monkeypatch.setattr(executor, "get_settings", lambda: disabled)

    executor.start_cleanup_loop()
    assert executor._cleanup_thread is None


def test_cleanup_loop_survives_a_failing_round(monkeypatch, test_settings):
    """单轮失败不能让线程退出：清理是维护动作，网络抖一下不该永久停摆。"""
    from app.tasks import cleanup, executor

    monkeypatch.setattr(executor, "get_settings", lambda: test_settings)
    monkeypatch.setattr(cleanup, "get_settings", lambda: test_settings)
    # sweep 用的是 `from app.database import SessionLocal` 的懒导入，故障要打在源头才生效
    monkeypatch.setattr("app.database.SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    executor.start_cleanup_loop(interval_seconds=1)
    try:
        # 线程起来后至少跑过一轮（那一轮会抛异常），仍然活着
        import time

        time.sleep(2.5)
        assert executor._cleanup_thread is not None
        assert executor._cleanup_thread.is_alive()
    finally:
        executor.stop_cleanup_loop()
