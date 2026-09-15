"""TOS 实现测试：用桩替换 tos SDK，验证参数传递、异常映射与凭据脱敏。

本机无真实凭据，真实连通性在测试环境以 `python -m app.storage.verify` 验证。
"""

import logging
import sys
import types
from pathlib import Path

import pytest

from app.storage.base import StorageClientError, StorageConfig, StorageServerError
from app.storage import tos_backend
from app.storage.tos_backend import TosStorage

CONFIG = StorageConfig(
    endpoint="tos-cn-beijing.volces.com",
    region="cn-beijing",
    bucket="bu-liverreview",
    access_key="AKID-LEAK-CANARY",
    secret_key="SECRET-LEAK-CANARY",
)


class FakeTosServerError(Exception):
    def __init__(self, message="server boom", code="AccessDenied", request_id="req-123", status_code=403):
        super().__init__(message)
        self.message = message
        self.code = code
        self.request_id = request_id
        self.status_code = status_code


class FakeTosClientError(Exception):
    def __init__(self, message="client boom"):
        super().__init__(message)
        self.message = message


class FakeHttpMethodType:
    Http_Method_Get = "GET"


class FakeClient:
    """记录调用参数的桩客户端。"""

    def __init__(self, ak, sk, endpoint, region):
        self.init_args = (ak, sk, endpoint, region)
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None

    def _record(self, *args):
        self.calls.append(args)
        if self.fail_with is not None:
            raise self.fail_with

    def put_object_from_file(self, bucket, key, local_path):
        self._record("put_object_from_file", bucket, key, local_path)

    def get_object(self, bucket, key):
        self._record("get_object", bucket, key)
        body = type("Body", (), {"read": lambda self_: b"tos-object-body"})()
        return type("Resp", (), {"read": lambda self_: body.read()})()

    def get_object_to_file(self, bucket, key, local_path):
        self._record("get_object_to_file", bucket, key, local_path)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"tos-object-body")

    def delete_object(self, bucket, key):
        self._record("delete_object", bucket, key)

    def pre_signed_url(self, method, bucket, key, expires=None):
        self._record("pre_signed_url", bucket, key, expires)
        return type("Presign", (), {"signed_url": f"https://signed.example/{key}?expires={expires}"})()


@pytest.fixture
def fake_tos(monkeypatch) -> types.SimpleNamespace:
    """把 tos 模块整体替换为桩，避免测试依赖真实 SDK 行为。"""
    created: list[FakeClient] = []

    def factory(ak, sk, endpoint, region):
        client = FakeClient(ak, sk, endpoint, region)
        created.append(client)
        return client

    fake = types.SimpleNamespace(
        TosClientV2=factory,
        HttpMethodType=FakeHttpMethodType,
        exceptions=types.SimpleNamespace(
            TosClientError=FakeTosClientError,
            TosServerError=FakeTosServerError,
        ),
    )
    monkeypatch.setitem(sys.modules, "tos", fake)
    monkeypatch.setattr(tos_backend, "_import_tos", lambda: fake)
    return types.SimpleNamespace(created=created)


@pytest.fixture
def storage(fake_tos) -> TosStorage:
    return TosStorage(CONFIG)


def test_client_is_lazy_and_created_once(storage, fake_tos):
    assert fake_tos.created == []
    storage.client  # 首次访问才创建
    storage.client
    assert len(fake_tos.created) == 1
    assert fake_tos.created[0].init_args == (
        CONFIG.access_key,
        CONFIG.secret_key,
        CONFIG.endpoint,
        CONFIG.region,
    )


def test_upload_passes_bucket_and_key(storage, tmp_path, fake_tos):
    payload = tmp_path / "a.ts"
    payload.write_bytes(b"x" * 16)
    key = "liverreview/original/a_1_00000000.ts"
    stored = storage.upload_file(payload, key)
    assert stored.object_key == key
    assert stored.size == 16
    assert fake_tos.created[0].calls[-1] == ("put_object_from_file", CONFIG.bucket, key, str(payload))


def test_missing_local_file_raises_client_error(storage):
    with pytest.raises(StorageClientError):
        storage.upload_file("不存在.ts", "liverreview/original/x.ts")


def test_server_error_is_mapped_with_request_id(storage, fake_tos):
    key = "liverreview/original/a_1_00000000.ts"
    storage.client.fail_with = FakeTosServerError()
    with pytest.raises(StorageServerError) as excinfo:
        storage.get_object_bytes(key)
    assert excinfo.value.code == "AccessDenied"
    assert excinfo.value.request_id == "req-123"
    assert excinfo.value.status_code == 403


def test_client_error_is_mapped(storage):
    storage.client.fail_with = FakeTosClientError()
    with pytest.raises(StorageClientError) as excinfo:
        storage.get_object_bytes("liverreview/original/a_1_00000000.ts")
    assert "client boom" in str(excinfo.value)


def test_delete_maps_not_found_to_server_error(storage):
    storage.client.fail_with = FakeTosServerError(code="NoSuchKey", request_id="req-404", status_code=404)
    with pytest.raises(StorageServerError):
        storage.delete_object("liverreview/clip/a_1_00000000.ts")


def test_presigned_url_uses_configured_ttl(storage, fake_tos):
    url = storage.generate_presigned_url("liverreview/clip/a.ts")
    assert url == f"https://signed.example/liverreview/clip/a.ts?expires={CONFIG.presigned_ttl_seconds}"


def test_download_creates_parent_directory(storage, tmp_path):
    target = tmp_path / "nested" / "out.ts"
    returned = storage.download_to_file("liverreview/clip/a.ts", target)
    assert returned == target
    assert target.read_bytes() == b"tos-object-body"


def test_credentials_never_appear_in_logs(storage, caplog):
    """AK/SK 与预签名 URL 不得进入日志。"""
    with caplog.at_level(logging.DEBUG):
        storage.generate_presigned_url("liverreview/clip/a.ts")
        storage.client.fail_with = FakeTosServerError()
        with pytest.raises(StorageServerError):
            storage.get_object_bytes("liverreview/clip/a.ts")
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert CONFIG.access_key not in joined
    assert CONFIG.secret_key not in joined
    assert "signed.example" not in joined


def test_credentials_never_appear_in_exception_text(storage):
    storage.client.fail_with = FakeTosServerError(message=f"denied for {CONFIG.access_key}")
    try:
        storage.get_object_bytes("liverreview/clip/a.ts")
    except StorageServerError as exc:
        assert CONFIG.access_key not in str(exc)
        assert CONFIG.secret_key not in str(exc)
    else:  # pragma: no cover - 若未抛出则测试无意义
        pytest.fail("预期抛出 StorageServerError")


def test_generate_object_key_uses_config_prefix(storage):
    key = storage.generate_object_key("original", "直播录屏.ts")
    assert key.startswith(f"{CONFIG.object_prefix}original/")
    assert key.endswith(".ts")


def test_sdk_missing_raises_client_error(monkeypatch):
    def boom():
        raise ImportError("No module named 'tos'")

    monkeypatch.setattr(tos_backend, "_import_tos", boom)
    storage = TosStorage(CONFIG)
    with pytest.raises(StorageClientError) as excinfo:
        storage.client
    assert "tos" in str(excinfo.value)
