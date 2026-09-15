"""本地 Mock 对象存储：内存实现，支持故障注入与固定时钟。

供本地开发、单元测试与自检命令使用；真实连通性验证在测试环境用 TosStorage 执行。
"""

from __future__ import annotations

from pathlib import Path

from app.storage.base import (
    ObjectStorage,
    StorageClientError,
    StorageConfig,
    StorageServerError,
    StoredObject,
    build_object_key,
)

_DEFAULT_CONFIG = StorageConfig(
    endpoint="mock-tos.local",
    region="mock-region",
    bucket="mock-bucket",
    access_key="mock-access-key",
    secret_key="mock-secret-key",
)


class FaultMode:
    """故障注入模式：用于在本地验证两类异常的处理路径。"""

    NONE = "none"
    CLIENT = "client"
    SERVER = "server"
    NOT_FOUND = "not_found"


class InMemoryStorage(ObjectStorage):
    """把对象保存在进程内存中，行为与真实后端一致但不触碰网络。"""

    def __init__(
        self,
        config: StorageConfig | None = None,
        *,
        now_ms: int | None = None,
        unique_token: str | None = None,
        fault_mode: str = FaultMode.NONE,
    ) -> None:
        self.config = config or _DEFAULT_CONFIG
        self._objects: dict[str, bytes] = {}
        self._now_ms = now_ms
        self._unique_token = unique_token
        self._fault_mode = fault_mode
        self._key_sequence = 0

    # —— 测试辅助 ——

    def set_fault_mode(self, mode: str) -> None:
        self._fault_mode = mode

    @property
    def uploaded_keys(self) -> list[str]:
        return list(self._objects.keys())

    @property
    def object_count(self) -> int:
        return len(self._objects)

    # —— 故障注入 ——

    def _maybe_fail(self, action: str) -> None:
        if self._fault_mode == FaultMode.CLIENT:
            raise StorageClientError(f"mock client failure during {action}")
        if self._fault_mode == FaultMode.SERVER:
            raise StorageServerError(
                f"mock server failure during {action}",
                code="MockServerError",
                request_id="mock-request-id-0001",
                status_code=500,
            )

    def _require(self, object_key: str) -> bytes:
        if self._fault_mode == FaultMode.NOT_FOUND:
            raise StorageServerError(
                f"object not found: {object_key}",
                code="NoSuchKey",
                request_id="mock-request-id-0002",
                status_code=404,
            )
        if object_key not in self._objects:
            raise StorageClientError(f"object not found: {object_key}")
        return self._objects[object_key]

    # —— 契约实现 ——

    def upload_file(self, local_path: str | Path, object_key: str) -> StoredObject:
        self._maybe_fail("upload")
        path = Path(local_path)
        if not path.is_file():
            raise StorageClientError(f"本地文件不存在：{path}")
        data = path.read_bytes()
        self._objects[object_key] = data
        return StoredObject(object_key=object_key, size=len(data))

    def download_to_file(self, object_key: str, local_path: str | Path) -> Path:
        self._maybe_fail("download")
        data = self._require(object_key)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def get_object_bytes(self, object_key: str) -> bytes:
        self._maybe_fail("read")
        return self._require(object_key)

    def delete_object(self, object_key: str) -> None:
        self._maybe_fail("delete")
        self._objects.pop(object_key, None)

    def generate_presigned_url(self, object_key: str, expires_seconds: int | None = None) -> str:
        self._maybe_fail("presign")
        ttl = self.config.presigned_ttl_seconds if expires_seconds is None else int(expires_seconds)
        return (
            f"https://mock-tos.local/{self.config.bucket}/{object_key}"
            f"?X-Tos-Expires={ttl}&expires={ttl}"
        )

    def generate_object_key(self, kind: str, filename: str) -> str:
        unique = self._unique_token
        if unique is not None:
            # 固定 token 场景下（now_ms 与 unique_token 均冻结）追加自增序号，
            # 避免同一毫秒时钟下重复调用生成完全相同的对象键。
            unique = f"{unique}{self._key_sequence}"
            self._key_sequence += 1
        return build_object_key(
            self.config.object_prefix,
            kind,
            filename,
            now_ms=self._now_ms,
            unique=unique,
        )
