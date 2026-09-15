"""自检命令测试：用 Mock 存储 + 注入的 fetch 走完六步流程。"""

from app.storage.base import StorageServerError
from app.storage.mock import FaultMode, InMemoryStorage
from app.storage.verify import run_verification


class _FlakyDeleteConfirmStorage(InMemoryStorage):
    """模拟「删除已成功，但确认读取时返回瞬时 5xx（非 404）」的场景。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._deleted = False

    def delete_object(self, object_key: str) -> None:
        super().delete_object(object_key)
        self._deleted = True

    def get_object_bytes(self, object_key: str) -> bytes:
        if self._deleted:
            raise StorageServerError(
                "mock transient server error",
                code="InternalError",
                request_id="mock-request-id-9999",
                status_code=500,
            )
        return super().get_object_bytes(object_key)


def test_run_verification_passes_on_healthy_storage(monkeypatch, tmp_path):
    storage = InMemoryStorage(now_ms=1731657645123, unique_token="deadbeef")

    def fetch(url: str) -> bytes:
        # 从 URL 中还原对象键后读取内容，模拟真实 HTTP 拉取
        object_key = url.split("/mock-bucket/")[1].split("?")[0]
        return storage.get_object_bytes(object_key)

    steps = run_verification(storage, fetch_bytes=fetch, workdir=tmp_path)
    names = [name for name, _, _ in steps]
    assert names == [
        "配置与实现",
        "生成测试文件",
        "上传对象",
        "预签名 URL 可访问",
        "下载并校验内容",
        "删除对象",
    ]
    assert all(passed for _, passed, _ in steps)
    assert storage.object_count == 0


def test_run_verification_keeps_object_when_requested(monkeypatch, tmp_path):
    storage = InMemoryStorage(now_ms=1731657645123, unique_token="deadbeef")

    def fetch(url: str) -> bytes:
        object_key = url.split("/mock-bucket/")[1].split("?")[0]
        return storage.get_object_bytes(object_key)

    steps = run_verification(storage, fetch_bytes=fetch, workdir=tmp_path, keep=True)
    assert all(passed for _, passed, _ in steps)
    assert storage.object_count == 1


def test_failed_presigned_fetch_is_reported(tmp_path):
    storage = InMemoryStorage(now_ms=1731657645123, unique_token="deadbeef")

    def broken_fetch(url: str) -> bytes:
        raise OSError("connection refused")

    steps = run_verification(storage, fetch_bytes=broken_fetch, workdir=tmp_path)
    failed = {name: message for name, passed, message in steps if not passed}
    assert "预签名 URL 可访问" in failed
    assert "connection refused" in failed["预签名 URL 可访问"]


def test_content_mismatch_is_reported(tmp_path):
    storage = InMemoryStorage(now_ms=1731657645123, unique_token="deadbeef")

    steps = run_verification(storage, fetch_bytes=lambda url: b"tampered", workdir=tmp_path)
    failed = {name: message for name, passed, message in steps if not passed}
    assert "预签名 URL 可访问" in failed
    assert "SHA256" in failed["预签名 URL 可访问"]


def test_server_fault_is_reported_with_request_id(tmp_path):
    storage = InMemoryStorage(now_ms=1731657645123, unique_token="deadbeef")
    storage.set_fault_mode(FaultMode.SERVER)
    steps = run_verification(storage, fetch_bytes=lambda url: b"", workdir=tmp_path)
    failed = {name: message for name, passed, message in steps if not passed}
    assert "上传对象" in failed
    assert "mock-request-id-0001" in failed["上传对象"]


def test_verification_stops_after_upload_failure(tmp_path):
    storage = InMemoryStorage(now_ms=1731657645123, unique_token="deadbeef")
    storage.set_fault_mode(FaultMode.CLIENT)
    steps = run_verification(storage, fetch_bytes=lambda url: b"", workdir=tmp_path)
    names = [name for name, _, _ in steps]
    assert names[:3] == ["配置与实现", "生成测试文件", "上传对象"]
    assert not steps[2][1]
    # 上传失败后不再执行后续依赖对象的步骤
    assert "下载并校验内容" not in names


def test_delete_confirmation_with_transient_server_error_is_not_treated_as_deleted(tmp_path):
    # 删除后确认读取若遇到非 404 的服务端错误（如瞬时 5xx），不能当作「已删除」
    storage = _FlakyDeleteConfirmStorage(now_ms=1731657645123, unique_token="deadbeef")

    def fetch(url: str) -> bytes:
        object_key = url.split("/mock-bucket/")[1].split("?")[0]
        return storage.get_object_bytes(object_key)

    steps = run_verification(storage, fetch_bytes=fetch, workdir=tmp_path)
    failed = {name: message for name, passed, message in steps if not passed}
    assert "删除对象" in failed
    assert "mock-request-id-9999" in failed["删除对象"]
