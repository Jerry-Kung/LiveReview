"""V0.1.4 媒体探测链路的测试：本地副本、探测入库、失败可见、重试与中断恢复。

探测用 `tests/fake_ffprobe.py` 代替真实 ffprobe（本机不部署 FFmpeg），因此这里覆盖的是
「探测编排」而不是 ffprobe 自身的解析能力——解析由 `tests/media/` 用固定 JSON 覆盖。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import chunks as chunk_store
from app.media.paths import original_path, originals_dir
from app.storage.mock import FaultMode
from tests.conftest import CHUNK_SIZE
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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def probe_fails(monkeypatch):
    """让假 ffprobe 以「文件损坏」的方式失败。

    本地副本按任务 id 命名（原始文件名不出现在路径里），因此探测行为由环境变量控制，
    子进程会继承它。
    """
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_MODE", "bad-media")


@pytest.fixture
def probe_reports_no_streams(monkeypatch):
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_MODE", "no-streams")


def test_complete_upload_leaves_no_merge_temp_file(client, media_root):
    """合并产物转存为本地副本后，合并临时文件不应残留（否则同一份数据占两份磁盘）。"""
    data = _complete(client)

    assert not chunk_store.merged_path(media_root, data["upload_id"]).exists()
    assert originals_dir(media_root).is_dir()


def test_probe_stores_metadata_on_task(client, media_root):
    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "succeeded"
    assert task["progress"] == 100
    assert task["error"] is None

    metadata = task["metadata"]
    assert metadata is not None
    assert metadata["format_name"] == "mpegts"
    assert metadata["duration_seconds"] == 600.0
    assert metadata["video_codec"] == "h264"
    assert (metadata["width"], metadata["height"]) == (1920, 1080)
    assert metadata["frame_rate"] == "25/1"
    assert metadata["audio_codec"] == "aac"
    assert metadata["sample_rate"] == 48000
    assert metadata["channels"] == 2
    assert metadata["stream_count"] == 2
    assert metadata["bit_rate"] == 3500000
    assert metadata["probed_at"]


def test_probe_runs_against_the_local_copy(client, media_root, monkeypatch, tmp_path):
    """探测对象必须是上传留下的本地副本，而不是分片目录或合并临时文件。

    V0.1.5 起同一任务会在探测之后继续探测转封装产物与切片产物，因此这里断言的是
    「探测过原始副本」而非「只探测了一次」。
    """
    log = tmp_path / "probe-log.txt"
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_LOG", str(log))

    data = _complete(client)

    probed = [Path(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    expected = original_path(media_root, data["task_id"], "live.ts")
    assert expected in probed
    # 首个被探测的文件必须是本地副本：时间基准来自原始素材，不能先拿产物去算
    assert probed[0] == expected


def test_content_hash_matches_the_uploaded_bytes(client):
    """content_hash 是「探测的是哪个文件」的凭据：必须等于上传内容的 sha256。"""
    data = _complete(client)
    metadata = _task(client, data["task_id"])["metadata"]

    expected = b"".join(bytes([index]) * CHUNK_SIZE for index in range(3))
    assert metadata["content_hash"] == _sha256(expected)


def test_local_copy_is_removed_after_successful_processing(client, media_root):
    """处理成功即释放本地副本：测试环境不该被多场 2GB 文件长期占满。"""
    data = _complete(client)

    assert not original_path(media_root, data["task_id"], "live.ts").exists()


def test_local_copy_is_released_even_when_probe_fails(client, media_root, probe_fails):
    """探测失败同样释放本地副本（V0.6.1 起）。

    此前失败会保留副本以便在容器内复跑 ffprobe；代价是失败任务会长期占着 GB 级文件，
    测试环境很快就会满。改为一律释放，复现时从对象存储重新取回（见
    `test_processing_redownloads_source_when_local_copy_is_missing`）。
    """
    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "failed"
    assert "ffprobe 探测失败" in task["error"]
    assert task["metadata"] is None
    assert not original_path(media_root, data["task_id"], "live.ts").exists()


def test_probe_failure_reason_is_visible_and_retryable(client, media_root, probe_fails):
    """失败原因可见并可重试；重试时本地副本仍在，不需要回下载。"""
    data = _complete(client)
    assert _task(client, data["task_id"])["status"] == "failed"

    retry = client.post(f"/api/tasks/{data['task_id']}/retry")
    assert retry.status_code == 200, retry.text
    # 假 ffprobe 对同一文件名依旧失败：重试确实重新执行了探测，而不是直接判成功
    assert _task(client, data["task_id"])["status"] == "failed"


def test_retry_clears_stale_metadata(client, db_session_factory, media_root, probe_fails):
    """重试前清空上一次的元数据，避免失败任务的界面残留陈旧结论。"""
    from app.models import Task, utcnow
    from app.tasks import STATUS_FAILED, reset_task_for_retry

    data = _complete(client)
    db = db_session_factory()
    try:
        task = db.get(Task, data["task_id"])
        task.status = STATUS_FAILED
        task.duration_seconds = 123.0
        task.video_codec = "h264"
        task.metadata_json = "{}"
        task.probed_at = utcnow()
        db.commit()

        reset = reset_task_for_retry(db, data["task_id"])
        assert reset is not None
        assert reset.duration_seconds is None
        assert reset.video_codec is None
        assert reset.metadata_json is None
        assert reset.probed_at is None
        assert reset.status == "uploaded"
    finally:
        db.close()


def test_probe_downloads_back_when_local_copy_is_gone(client, storage, media_root, db_session_factory):
    """本地副本与转封装产物都已清理时，处理从对象存储取回原始文件。"""
    from app.media.paths import converted_path
    from app.models import Task, utcnow

    data = _complete(client)
    task_id = data["task_id"]
    assert not original_path(media_root, task_id, "live.ts").exists()
    # 转封装产物也清掉，强制走「副本缺失 → 从对象存储取回」这一条路径
    converted_path(media_root, task_id).unlink(missing_ok=True)

    db = db_session_factory()
    try:
        # 先退回「已失败」以走通重试入口，再由重试接口完成重置
        task = db.get(Task, task_id)
        task.status = "failed"
        task.error = "模拟失败"
        task.updated_at = utcnow()
        db.commit()
    finally:
        db.close()

    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 200

    latest = _task(client, task_id)
    assert latest["status"] == "succeeded"
    assert latest["metadata"]["duration_seconds"] == 600.0
    # 处理成功后副本又被清理
    assert not original_path(media_root, task_id, "live.ts").exists()


def test_probe_reports_clear_reason_when_object_is_missing(client, storage, media_root, db_session_factory):
    """副本缺失且对象取不回来时，失败原因要指向「取回原始视频」这一步。"""
    from app.models import Task, utcnow

    data = _create(client)
    task_id = data["task_id"]
    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.status = "failed"
        task.object_key = "liverreview/original/absent.ts"
        task.updated_at = utcnow()
        db.commit()
    finally:
        db.close()

    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 200

    latest = _task(client, task_id)
    assert latest["status"] == "failed"
    assert "Storage" in latest["error"] or "对象" in latest["error"]


def test_probe_rejects_media_without_playable_streams(client, media_root, probe_reports_no_streams):
    """只有数据流（无音视频）的文件不能算探测成功，否则下游会拿到一份没用的元数据。"""
    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "failed"
    assert "没有音频或视频流" in task["error"]


def test_task_without_metadata_returns_null(client):
    """未探测的任务不返回空元数据对象：前端据此区分「未探测」与「探测出空值」。"""
    data = _create(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "uploading"
    assert task["metadata"] is None


def test_delete_removes_local_copy(client, storage, media_root, db_session_factory):
    """删除任务时本地副本一并清理，不留孤儿文件占磁盘。"""
    from app.models import Task, utcnow

    data = _create(client)
    task_id = data["task_id"]
    _upload_all(client, data["upload_id"])

    # 手工造一个本地副本并让任务停在失败态（模拟探测失败后用户直接删除）
    copy = original_path(media_root, task_id, "live.ts")
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"local-copy")
    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.status = "failed"
        task.updated_at = utcnow()
        db.commit()
    finally:
        db.close()

    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert not copy.exists()


def test_stale_processing_tasks_are_reclaimed_on_startup(client, db_session_factory):
    """进程重启后残留的 processing 任务必须被退回 uploaded，否则永远卡住。"""
    from app.models import Task
    from app.tasks import STATUS_PROCESSING, STATUS_UPLOADED, reclaim_stale_processing

    db = db_session_factory()
    try:
        db.add(Task(id="stale1", kind="ingest", status=STATUS_PROCESSING, progress=20, process_token="tok"))
        db.commit()

        assert reclaim_stale_processing(db) == 1

        task = db.get(Task, "stale1")
        assert task.status == STATUS_UPLOADED
        assert task.process_token is None
        assert task.progress == 0
        # 已经处于 uploaded 的任务不受影响
        assert reclaim_stale_processing(db) == 0
    finally:
        db.close()


def test_probe_failure_does_not_block_other_tasks(
    client, storage, media_root, probe_fails, monkeypatch
):
    """一场素材探测失败不应影响后续上传：失败只作用于自己的任务与副本。"""
    bad = _complete(client)
    monkeypatch.delenv("LIVERREVIEW_FAKE_PROBE_MODE")
    good = _complete(client)

    assert _task(client, bad["task_id"])["status"] == "failed"
    assert _task(client, good["task_id"])["status"] == "succeeded"
    # 无论成败都不留本地副本（V0.6.1）：失败那场不会占着磁盘影响后续上传
    assert not original_path(media_root, bad["task_id"], "live.ts").exists()
    assert not original_path(media_root, good["task_id"], "live.ts").exists()


def test_storage_fault_during_probe_fails_with_request_id(client, storage, media_root, db_session_factory):
    """探测阶段的对象存储故障同样要落库原因与服务端 request_id，便于向服务商定位。"""
    from app.models import Task, utcnow

    data = _create(client)
    task_id = data["task_id"]
    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.status = "failed"
        task.object_key = "liverreview/original/live_x.ts"
        task.updated_at = utcnow()
        db.commit()
    finally:
        db.close()

    storage.set_fault_mode(FaultMode.SERVER)
    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 200

    latest = _task(client, task_id)
    assert latest["status"] == "failed"
    assert "mock-request-id-0001" in latest["error"]
