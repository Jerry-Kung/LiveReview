"""上传与任务链路的接口测试：存储用 Mock，任务执行器同步执行。

覆盖 v0.1.3 的本地验证要点：分片乱序与续传、缺片被拒、合并后落存储、失败原因可见与重试。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import chunks as chunk_store
from app.media.paths import clips_dir, converted_path, original_path
from app.storage.mock import FaultMode
from tests.conftest import CHUNK_SIZE, FILE_SIZE


def _create(client: TestClient, filename: str = "live.ts", size: int = FILE_SIZE) -> dict:
    resp = client.post("/api/uploads", json={"filename": filename, "size": size})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _chunk_body(
    index: int, declared_size: int = FILE_SIZE, chunk_size: int = CHUNK_SIZE
) -> bytes:
    return bytes([index]) * chunk_store.expected_chunk_size(declared_size, chunk_size, index)


def _upload_all(client: TestClient, upload_id: str, total: int = 3) -> None:
    for index in range(total):
        resp = client.put(f"/api/uploads/{upload_id}/chunks/{index}", content=_chunk_body(index))
        assert resp.status_code == 204, resp.text


def test_create_upload_returns_chunk_plan(client):
    data = _create(client)
    assert data["chunk_size"] == CHUNK_SIZE
    assert data["total_chunks"] == 3
    assert data["received_chunks"] == []
    assert data["status"] == "uploading"
    assert data["task_id"]


def test_create_upload_rejects_non_positive_size(client):
    resp = client.post("/api/uploads", json={"filename": "live.ts", "size": 0})
    assert resp.status_code == 422


def test_chunk_upload_accepts_out_of_order_and_reports_received(client):
    data = _create(client)
    upload_id = data["upload_id"]

    assert client.put(f"/api/uploads/{upload_id}/chunks/2", content=_chunk_body(2)).status_code == 204
    assert client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0)).status_code == 204

    body = client.get(f"/api/uploads/{upload_id}").json()
    assert body["received_chunks"] == [0, 2]
    assert body["total_chunks"] == 3


def test_chunk_upload_overwrites_same_index(client):
    data = _create(client)
    upload_id = data["upload_id"]

    assert client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0)).status_code == 204
    assert client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0)).status_code == 204

    body = client.get(f"/api/uploads/{upload_id}").json()
    # 重复上传同一个分片不重复计数
    assert body["received_chunks"] == [0]
    assert body["received_bytes"] == CHUNK_SIZE


def test_chunk_upload_rejects_wrong_size(client):
    data = _create(client)
    upload_id = data["upload_id"]

    resp = client.put(f"/api/uploads/{upload_id}/chunks/0", content=b"short")
    assert resp.status_code == 400
    assert "大小异常" in resp.json()["detail"]


def test_chunk_upload_accepts_short_final_chunk(client):
    """末片不足一整片时必须按期望值放行，否则大文件永远无法收尾。"""
    declared = CHUNK_SIZE + 100
    data = _create(client, size=declared)
    upload_id = data["upload_id"]
    assert data["total_chunks"] == 2

    body = _chunk_body(1, declared_size=declared)
    assert len(body) == 100
    assert client.put(f"/api/uploads/{upload_id}/chunks/1", content=body).status_code == 204
    assert client.put(
        f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0, declared_size=declared)
    ).status_code == 204

    resp = client.post(f"/api/uploads/{upload_id}/complete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["size"] == declared


def test_chunk_upload_rejects_out_of_range_index(client):
    data = _create(client)
    upload_id = data["upload_id"]

    resp = client.put(f"/api/uploads/{upload_id}/chunks/9", content=_chunk_body(0))
    assert resp.status_code == 400
    assert "超出范围" in resp.json()["detail"]


def test_complete_reports_missing_chunks(client):
    data = _create(client)
    upload_id = data["upload_id"]

    assert client.put(f"/api/uploads/{upload_id}/chunks/1", content=_chunk_body(1)).status_code == 204
    resp = client.post(f"/api/uploads/{upload_id}/complete")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["missing_chunks"] == [0, 2]
    assert detail["received_chunks"] == [1]
    assert detail["total_chunks"] == 3


def test_complete_upload_then_resume_and_finish(client, media_root):
    """中断后补齐缺片即可完成：同一次上传会话内续传。"""
    data = _create(client)
    upload_id = data["upload_id"]

    assert client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0)).status_code == 204
    assert client.post(f"/api/uploads/{upload_id}/complete").status_code == 409

    # 重新查询已收分片，跳过第 0 片后补齐剩余分片
    received = client.get(f"/api/uploads/{upload_id}").json()["received_chunks"]
    assert received == [0]
    for index in (1, 2):
        assert client.put(
            f"/api/uploads/{upload_id}/chunks/{index}", content=_chunk_body(index)
        ).status_code == 204

    assert client.post(f"/api/uploads/{upload_id}/complete").status_code == 200
    assert chunk_store.received_indices(media_root, upload_id) == []


def test_complete_uploads_object_and_finishes_task(client, storage, media_root):
    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    _upload_all(client, upload_id)

    resp = client.post(f"/api/uploads/{upload_id}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["size"] == FILE_SIZE

    object_key = body["object_key"]
    assert object_key in storage.uploaded_keys
    assert object_key.startswith("liverreview/original/live_")
    assert storage.get_object_bytes(object_key) == b"".join(_chunk_body(i) for i in range(3))

    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    assert task["object_key"] == object_key
    assert task["size"] == FILE_SIZE
    assert task["filename"] == "live.ts"
    assert task["error"] is None

    session = client.get(f"/api/uploads/{upload_id}").json()
    assert session["status"] == "completed"
    # 分片目录与合并临时文件均已清理
    assert chunk_store.received_indices(media_root, upload_id) == []
    assert not chunk_store.merged_path(media_root, upload_id).exists()


def test_complete_is_guarded_against_double_submit(client):
    data = _create(client)
    upload_id = data["upload_id"]
    _upload_all(client, upload_id)

    assert client.post(f"/api/uploads/{upload_id}/complete").status_code == 200
    second = client.post(f"/api/uploads/{upload_id}/complete")
    assert second.status_code == 409
    assert "已完成" in second.json()["detail"]


def test_chunk_upload_rejected_after_complete(client):
    data = _create(client)
    upload_id = data["upload_id"]
    _upload_all(client, upload_id)
    client.post(f"/api/uploads/{upload_id}/complete")

    resp = client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0))
    assert resp.status_code == 409


def test_storage_failure_marks_upload_and_task_failed(client, storage):
    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    _upload_all(client, upload_id)
    storage.set_fault_mode(FaultMode.SERVER)

    resp = client.post(f"/api/uploads/{upload_id}/complete")
    assert resp.status_code == 502
    assert "上传失败" in resp.json()["detail"]

    session = client.get(f"/api/uploads/{upload_id}").json()
    assert session["status"] == "failed"
    # 失败原因可见，且带服务端 request_id 便于向服务商定位
    assert "mock-request-id-0001" in session["error"]

    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert "mock-request-id-0001" in task["error"]


def test_storage_failure_keeps_chunks_for_retry(client, storage, media_root):
    """入库失败要保留分片：合并临时文件清掉，但已传分片不丢。

    否则一次入库失败就要用户重传整个 1～2GB 原文件，与「失败可重新发起」的验收意图相悖。
    """
    data = _create(client)
    upload_id = data["upload_id"]
    _upload_all(client, upload_id)
    storage.set_fault_mode(FaultMode.SERVER)

    client.post(f"/api/uploads/{upload_id}/complete")

    assert chunk_store.received_indices(media_root, upload_id) == [0, 1, 2]
    assert chunk_store.received_bytes(media_root, upload_id) == FILE_SIZE
    assert not chunk_store.merged_path(media_root, upload_id).exists()


def test_failed_upload_accepts_missing_chunks_again_and_completes(client, storage, media_root):
    """失败后重新提交分片视为继续本次上传：无需重传已成功的分片。"""
    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    _upload_all(client, upload_id)
    storage.set_fault_mode(FaultMode.SERVER)
    client.post(f"/api/uploads/{upload_id}/complete")

    assert client.get(f"/api/uploads/{upload_id}").json()["status"] == "failed"

    storage.set_fault_mode(FaultMode.NONE)
    # 会话退回 uploading 并清掉旧原因，前端可据此重新提交完成请求
    assert client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0)).status_code == 204
    reopened = client.get(f"/api/uploads/{upload_id}").json()
    assert reopened["status"] == "uploading"
    assert reopened["error"] is None

    assert client.post(f"/api/uploads/{upload_id}/complete").status_code == 200
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "succeeded"


def test_retry_rejected_for_task_whose_upload_failed(client, storage):
    """原始视频都没落存储的任务不该被判为可重试：该走重新上传，而非重试任务。"""
    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    _upload_all(client, upload_id)

    storage.set_fault_mode(FaultMode.SERVER)
    client.post(f"/api/uploads/{upload_id}/complete")

    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["object_key"] is None

    storage.set_fault_mode(FaultMode.NONE)
    resp = client.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 409
    assert "只有已入库且失败的任务可以重新执行" in resp.json()["detail"]


def test_retry_after_processing_failure_succeeds(client, storage, db_session_factory, media_root, tmp_path):
    """已落存储的任务在后台处理失败后可重试，且复用同一对象，不重复上传。

    重试时上传留下的本地副本已被清理，探测会按对象键从存储取回，因此这里预置对象内容。
    """
    from app.models import Task, utcnow

    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    _upload_all(client, upload_id)

    storage.set_fault_mode(FaultMode.SERVER)
    client.post(f"/api/uploads/{upload_id}/complete")
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "failed"

    # 模拟「原始视频已入库、后台处理阶段失败」这一场景：对象在存储中，本地副本已被清理
    storage.set_fault_mode(FaultMode.NONE)
    object_key = "liverreview/original/live_1_abcdabcd.ts"
    source = tmp_path / "stored.ts"
    source.write_bytes(b"stored-original")
    storage.upload_file(source, object_key)
    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.object_key = object_key
        task.size = FILE_SIZE
        task.status = "failed"
        task.error = "探测失败：mock"
        task.updated_at = utcnow()
        db.commit()
    finally:
        db.close()
    assert not original_path(media_root, task_id, "live.ts").exists()

    retry = client.post(f"/api/tasks/{task_id}/retry")
    assert retry.status_code == 200, retry.text

    latest = client.get(f"/api/tasks/{task_id}").json()
    assert latest["status"] == "succeeded"
    assert latest["error"] is None
    assert latest["object_key"] == object_key
    assert latest["metadata"]["duration_seconds"] == 600.0
    # 对象已在存储中，重试不重复上传原始视频；切出的片段是本轮新增的对象
    assert storage.uploaded_keys[0] == object_key
    assert len([key for key in storage.uploaded_keys if "/clip/" in key]) == len(latest["clips"])

    # 探测成功后再删掉本地副本与对象的临时夹具内容，避免影响后续断言
    client.delete(f"/api/tasks/{task_id}")


def test_retry_rejected_for_non_failed_task(client):
    data = _create(client)
    resp = client.post(f"/api/tasks/{data['task_id']}/retry")
    assert resp.status_code == 409
    assert "只有已入库且失败的任务可以重新执行" in resp.json()["detail"]


def test_task_listing_shows_history(client):
    first = _create(client, filename="a.ts")
    _create(client, filename="b.ts")

    items = client.get("/api/tasks").json()["items"]
    assert len(items) == 2
    assert {item["status"] for item in items} == {"uploading"}

    detail = client.get(f"/api/tasks/{first['task_id']}").json()
    assert detail["filename"] == "a.ts"
    assert detail["progress"] == 0


def test_unknown_upload_and_task_return_404(client):
    assert client.get("/api/uploads/nope").status_code == 404
    assert client.get("/api/tasks/nope").status_code == 404
    assert client.put("/api/uploads/nope/chunks/0", content=b"x").status_code == 404


def test_claim_is_exclusive(db_session_factory):
    """认领式流转：第二个执行器无法认领已被认领的任务，避免重复执行。"""
    from app.models import Task
    from app.tasks import STATUS_PROCESSING, STATUS_UPLOADED, claim_task

    db = db_session_factory()
    try:
        db.add(Task(id="t1", kind="ingest", status=STATUS_UPLOADED, progress=0))
        db.commit()

        first = claim_task(db, "t1")
        second = claim_task(db, "t1")

        assert first is not None
        assert first.status == STATUS_PROCESSING
        assert first.process_token
        assert second is None
    finally:
        db.close()


def test_claim_skips_terminal_task(db_session_factory):
    from app.models import Task
    from app.tasks import STATUS_SUCCEEDED, claim_task

    db = db_session_factory()
    try:
        db.add(Task(id="t2", kind="ingest", status=STATUS_SUCCEEDED, progress=100))
        db.commit()
        assert claim_task(db, "t2") is None
    finally:
        db.close()


def test_task_without_object_key_fails_with_reason(db_session_factory):
    """任务体缺少必要信息时必须失败并落库原因，而不是静默成功。"""
    from app.models import Task
    from app.tasks import STATUS_FAILED, STATUS_UPLOADED, run_task

    db = db_session_factory()
    try:
        db.add(Task(id="t3", kind="ingest", status=STATUS_UPLOADED, progress=0))
        db.commit()

        run_task(db, "t3")

        task = db.get(Task, "t3")
        assert task.status == STATUS_FAILED
        assert "对象键" in task.error
    finally:
        db.close()


def test_move_merged_to_original_falls_back_to_copy_on_cross_device(tmp_path):
    """跨设备时 Path.replace 不可用，转存必须退化为复制并清掉源文件，而不是直接报错。"""
    merged = tmp_path / "assembling" / "u1.part"
    merged.parent.mkdir(parents=True)
    merged.write_bytes(b"media-bytes")
    target = tmp_path / "originals" / "t1.ts"

    with patch.object(Path, "replace", side_effect=OSError(18, "Invalid cross-device link")):
        result = chunk_store.move_merged_to_original(merged, target)

    assert result == target
    assert target.read_bytes() == b"media-bytes"
    assert not merged.exists()


def test_move_merged_to_original_reports_failure_when_copy_also_fails(tmp_path):
    """转存彻底失败必须抛 UploadError：副本是探测的唯一本地输入，不能静默放过。"""
    merged = tmp_path / "assembling" / "u1.part"
    merged.parent.mkdir(parents=True)
    merged.write_bytes(b"media-bytes")
    target = tmp_path / "originals" / "t1.ts"

    with patch.object(Path, "replace", side_effect=OSError(18, "cross-device")):
        with patch("app.chunks.shutil.copy2", side_effect=OSError(28, "No space left on device")):
            with pytest.raises(chunk_store.UploadError):
                chunk_store.move_merged_to_original(merged, target)


def test_concurrent_chunk_uploads_keep_all_bytes(client, media_root):
    """并发上传分片：同一会话下多片并行写入不丢片、不串号。"""
    from app.main import app

    data = _create(client)
    upload_id = data["upload_id"]

    async def upload_all() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            responses = await asyncio.gather(
                *[
                    ac.put(f"/api/uploads/{upload_id}/chunks/{index}", content=_chunk_body(index))
                    for index in range(3)
                ]
            )
        return [resp.status_code for resp in responses]

    assert asyncio.run(upload_all()) == [204, 204, 204]
    assert chunk_store.received_indices(media_root, upload_id) == [0, 1, 2]
    assert chunk_store.received_bytes(media_root, upload_id) == FILE_SIZE


def test_requeue_pending_picks_up_uploaded_tasks(db_session_factory, monkeypatch):
    """进程重启后待处理任务可被重新入队（覆盖执行器丢任务的场景）。"""
    from app.models import Task
    from app.tasks import STATUS_UPLOADED, executor, requeue_pending

    db = db_session_factory()
    try:
        db.add(Task(id="t4", kind="ingest", status=STATUS_UPLOADED, progress=0))
        db.add(Task(id="t5", kind="ingest", status="uploading", progress=0))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(executor, "SessionLocal", db_session_factory)
    submitted: list[str] = []
    monkeypatch.setattr(executor, "submit", submitted.append)

    assert requeue_pending() == 1
    assert submitted == ["t4"]


def test_requeue_pending_reclaims_tasks_stuck_in_processing(db_session_factory, monkeypatch):
    """进程被杀后停在 processing 的任务也必须被重新入队，否则永远卡住。

    认领凭据（process_token）是内存态的，重启后一定失效，因此这类任务必须重置而不是跳过。
    """
    from app.models import Task
    from app.tasks import STATUS_PROCESSING, STATUS_UPLOADED, executor, requeue_pending

    db = db_session_factory()
    try:
        db.add(Task(id="t6", kind="ingest", status=STATUS_PROCESSING, progress=20, process_token="tok"))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(executor, "SessionLocal", db_session_factory)
    submitted: list[str] = []
    monkeypatch.setattr(executor, "submit", submitted.append)

    assert requeue_pending() == 1
    assert submitted == ["t6"]

    db = db_session_factory()
    try:
        task = db.get(Task, "t6")
        assert task.status == STATUS_UPLOADED
        assert task.process_token is None
        assert task.progress == 0
    finally:
        db.close()


# —— 删除视频（v0.1.3.1）——


def _upload_one(client, storage) -> tuple[str, str, str]:
    """走完一次上传，返回 (upload_id, task_id, object_key)。"""
    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    _upload_all(client, upload_id)
    body = client.post(f"/api/uploads/{upload_id}/complete").json()
    return upload_id, task_id, body["object_key"]


def test_delete_removes_object_records_and_chunks(client, storage, media_root, db_session_factory):
    upload_id, task_id, object_key = _upload_one(client, storage)
    task = client.get(f"/api/tasks/{task_id}").json()
    clip_count = len(task["clips"])
    assert clip_count > 0
    # 云端对象 = 原始视频 + 各切片
    assert storage.object_count == 1 + clip_count

    resp = client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 204, resp.text

    # 云端对象（原始视频与全部切片）已删除
    assert storage.object_count == 0
    # 任务与上传会话记录均已删除
    assert client.get(f"/api/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/uploads/{upload_id}").status_code == 404
    assert client.get("/api/tasks").json()["items"] == []

    from app.models import MediaClip, Task, UploadSession

    db = db_session_factory()
    try:
        assert db.get(Task, task_id) is None
        # 片段行随任务级联删除
        assert list(db.query(MediaClip).filter(MediaClip.task_id == task_id)) == []
        assert db.get(UploadSession, upload_id) is None
    finally:
        db.close()

    # 本地分片目录、合并临时文件与切片目录均不残留
    assert not chunk_store.chunks_dir(media_root, upload_id).exists()
    assert not chunk_store.merged_path(media_root, upload_id).exists()
    assert not clips_dir(media_root, task_id).exists()
    # 原始副本与转封装产物也不残留
    assert not original_path(media_root, task_id, "live.ts").exists()
    assert not converted_path(media_root, task_id).exists()


def test_delete_unknown_task_returns_404(client):
    assert client.delete("/api/tasks/nope").status_code == 404


def test_delete_is_not_repeatable(client, storage):
    """删除是破坏性操作：重复删除必须返回 404，而不是静默成功掩盖误操作。"""
    _, task_id, _ = _upload_one(client, storage)

    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert client.delete(f"/api/tasks/{task_id}").status_code == 404


def test_delete_rejected_while_upload_in_progress(client, media_root):
    """上传中的任务被拒：此时原始视频尚未落存储，删除会让分片与会话状态互相打架。"""
    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    client.put(f"/api/uploads/{upload_id}/chunks/0", content=_chunk_body(0))

    resp = client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 409
    assert "无法删除" in resp.json()["detail"]
    # 记录与分片原样保留
    assert client.get(f"/api/tasks/{task_id}").status_code == 200
    assert chunk_store.received_indices(media_root, upload_id) == [0]


def test_delete_rejected_while_processing(client, db_session_factory):
    """处理中的任务被拒：执行器可能正在读写该对象。"""
    data = _create(client)
    task_id = data["task_id"]

    from app.models import Task
    from app.tasks import STATUS_PROCESSING

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.status = STATUS_PROCESSING
        task.process_token = "token-1"
        db.commit()
    finally:
        db.close()

    resp = client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 409
    assert "processing" in resp.json()["detail"]
    assert client.get(f"/api/tasks/{task_id}").status_code == 200


def test_delete_storage_failure_keeps_records(client, storage):
    """云端删除失败时保留记录：否则对象留在桶里且再无任何线索可定位。"""
    _, task_id, object_key = _upload_one(client, storage)
    storage.set_fault_mode(FaultMode.SERVER)

    resp = client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 502
    assert "删除失败" in resp.json()["detail"]
    assert "mock-request-id-0001" in resp.json()["detail"]

    # 记录与对象都还在，重新发起删除仍可成功
    assert client.get(f"/api/tasks/{task_id}").json()["object_key"] == object_key
    storage.set_fault_mode(FaultMode.NONE)
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert storage.object_count == 0


def test_delete_failed_upload_clears_leftover_chunks(client, storage, media_root):
    """入库失败的任务没有对象键，但本地留有分片，删除要能清掉这些残留。"""
    data = _create(client)
    upload_id = data["upload_id"]
    task_id = data["task_id"]
    _upload_all(client, upload_id)
    storage.set_fault_mode(FaultMode.SERVER)
    client.post(f"/api/uploads/{upload_id}/complete")
    assert chunk_store.received_indices(media_root, upload_id) == [0, 1, 2]

    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert not chunk_store.chunks_dir(media_root, upload_id).exists()
    assert client.get(f"/api/tasks/{task_id}").status_code == 404
