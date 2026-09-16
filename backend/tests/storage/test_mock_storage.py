"""本地 Mock 存储测试：往返一致性、幂等删除与两类故障注入。"""

import hashlib
from pathlib import Path

import pytest

from app.storage.base import StorageClientError, StorageServerError
from app.storage.mock import FaultMode, InMemoryStorage


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage(now_ms=1731657645123, unique_token="deadbeef")


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    path = tmp_path / "sample.ts"
    path.write_bytes(b"liverreview-mock-payload" * 8)
    return path


def test_upload_then_read_round_trip(storage, payload):
    key = storage.generate_object_key("original", payload.name)
    stored = storage.upload_file(payload, key)
    assert stored.object_key == key
    assert stored.size == payload.stat().st_size
    assert storage.get_object_bytes(key) == payload.read_bytes()


def test_download_to_file_creates_parent_directory(storage, payload, tmp_path):
    key = storage.generate_object_key("clip", payload.name)
    storage.upload_file(payload, key)
    target = tmp_path / "nested" / "out" / "clip.ts"
    returned = storage.download_to_file(key, target)
    assert returned == target
    assert target.read_bytes() == payload.read_bytes()


def test_downloaded_content_matches_uploaded_checksum(storage, payload, tmp_path):
    key = storage.generate_object_key("clip", payload.name)
    storage.upload_file(payload, key)
    target = tmp_path / "verify.ts"
    storage.download_to_file(key, target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(
        payload.read_bytes()
    ).hexdigest()


def test_delete_is_idempotent(storage, payload):
    key = storage.generate_object_key("original", payload.name)
    storage.upload_file(payload, key)
    storage.delete_object(key)
    assert storage.object_count == 0
    storage.delete_object(key)  # 再次删除不抛异常


def test_presigned_url_contains_key_and_expiry(storage, payload):
    key = storage.generate_object_key("original", payload.name)
    storage.upload_file(payload, key)
    url = storage.generate_presigned_url(key, expires_seconds=120)
    assert key in url
    assert "expires=120" in url
    assert url.startswith("https://mock-tos.local/")


def test_presigned_url_uses_config_default_ttl(storage, payload):
    key = storage.generate_object_key("original", payload.name)
    storage.upload_file(payload, key)
    assert f"expires={storage.config.presigned_ttl_seconds}" in storage.generate_presigned_url(key)


def test_missing_object_raises_server_error(storage):
    with pytest.raises(StorageServerError) as excinfo:
        storage.get_object_bytes("liverreview/clip/absent_1_00000000.ts")
    assert excinfo.value.code == "NoSuchKey"
    assert excinfo.value.status_code == 404


def test_client_fault_injection_raises_client_error(storage, payload):
    key = storage.generate_object_key("original", payload.name)
    storage.set_fault_mode(FaultMode.CLIENT)
    with pytest.raises(StorageClientError) as excinfo:
        storage.upload_file(payload, key)
    assert "mock client failure" in str(excinfo.value)


def test_server_fault_injection_raises_server_error_with_request_id(storage, payload):
    key = storage.generate_object_key("clip", payload.name)
    storage.upload_file(payload, key)
    storage.set_fault_mode(FaultMode.SERVER)
    with pytest.raises(StorageServerError) as excinfo:
        storage.get_object_bytes(key)
    assert excinfo.value.code == "MockServerError"
    assert excinfo.value.request_id == "mock-request-id-0001"
    assert excinfo.value.status_code == 500


def test_server_fault_does_not_write_object(storage, payload):
    key = storage.generate_object_key("original", payload.name)
    storage.set_fault_mode(FaultMode.SERVER)
    with pytest.raises(StorageServerError):
        storage.upload_file(payload, key)
    storage.set_fault_mode(FaultMode.NONE)
    assert storage.object_count == 0


def test_not_found_fault_mode_maps_to_server_error(storage, payload):
    key = storage.generate_object_key("clip", payload.name)
    storage.upload_file(payload, key)
    storage.set_fault_mode(FaultMode.NOT_FOUND)
    with pytest.raises(StorageServerError) as excinfo:
        storage.get_object_bytes(key)
    assert excinfo.value.code == "NoSuchKey"
    assert excinfo.value.status_code == 404


def test_uploaded_keys_track_upload_order(storage, payload):
    first = storage.generate_object_key("original", "a.ts")
    second = storage.generate_object_key("clip", "b.ts")
    storage.upload_file(payload, first)
    storage.upload_file(payload, second)
    assert storage.uploaded_keys == [first, second]


def test_uploaded_keys_persist_after_delete(storage, payload):
    key = storage.generate_object_key("original", payload.name)
    storage.upload_file(payload, key)
    storage.delete_object(key)
    assert storage.object_count == 0
    assert storage.uploaded_keys == [key]  # 上传历史不因删除而消失


def test_frozen_clock_still_produces_distinct_keys():
    # 冻结时钟但使用默认随机 token：唯一性由随机 token 保证
    storage = InMemoryStorage(now_ms=1731657645123)
    keys = {storage.generate_object_key("clip", "same.ts") for _ in range(3)}
    assert len(keys) == 3
    assert all(k.startswith("liverreview/clip/same_1731657645123_") for k in keys)


# —— 大对象：注入一个很小的阈值，不真的写 32MiB 文件 ——


@pytest.fixture
def large_storage() -> InMemoryStorage:
    # 阈值调小为 16 字节，便于用小文件模拟「超过阈值的大对象」路径
    return InMemoryStorage(
        now_ms=1731657645123, unique_token="deadbeef", max_in_memory_object_bytes=16
    )


@pytest.fixture
def large_payload(tmp_path: Path) -> Path:
    path = tmp_path / "large.ts"
    path.write_bytes(b"liverreview-large-object-payload")  # 超过 16 字节阈值
    return path


def test_large_object_get_bytes_raises_client_error(large_storage, large_payload):
    key = large_storage.generate_object_key("original", large_payload.name)
    large_storage.upload_file(large_payload, key)
    with pytest.raises(StorageClientError) as excinfo:
        large_storage.get_object_bytes(key)
    assert "大对象" in str(excinfo.value)


def test_large_object_can_still_be_downloaded(large_storage, large_payload, tmp_path):
    key = large_storage.generate_object_key("original", large_payload.name)
    large_storage.upload_file(large_payload, key)
    target = tmp_path / "downloaded-large.ts"
    returned = large_storage.download_to_file(key, target)
    assert returned == target
    assert target.read_bytes() == large_payload.read_bytes()


def test_large_object_counts_toward_object_count(large_storage, large_payload):
    key = large_storage.generate_object_key("original", large_payload.name)
    large_storage.upload_file(large_payload, key)
    assert large_storage.object_count == 1
    large_storage.delete_object(key)
    assert large_storage.object_count == 0
