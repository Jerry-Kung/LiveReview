"""本地 Mock 对象存储：内存实现，支持故障注入与固定时钟。

供本地开发、单元测试与自检命令使用；真实连通性验证在测试环境用 TosStorage 执行。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
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

# Mock 只在内存中保留「小对象」的完整内容；超过此阈值的大对象只记录元数据
# （本地路径、大小、内容摘要），不把内容读入内存。真实录屏原始文件有 1-2GB，
# 若整体读入内存常驻，本地开发一次上传自测就会吃掉数 GB 内存；真实 TOS 后端的
# put_object_from_file 走流式传输，不受此限制，这里只是补齐 Mock 的对应行为。
_MAX_IN_MEMORY_OBJECT_BYTES = 32 * 1024 * 1024  # 32 MiB

# 流式计算摘要时的读取块大小：避免为算 SHA256 而整体读入大文件。
_HASH_CHUNK_SIZE = 1024 * 1024


def _sha256_file(path: Path, chunk_size: int = _HASH_CHUNK_SIZE) -> str:
    """流式计算文件内容的 SHA256，不整体读入内存。"""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass(frozen=True)
class _LargeObjectRecord:
    """大对象的元数据记录：不持有内容，只记录定位与校验所需信息。"""

    local_path: Path
    size: int
    digest: str


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
        max_in_memory_object_bytes: int = _MAX_IN_MEMORY_OBJECT_BYTES,
    ) -> None:
        self.config = config or _DEFAULT_CONFIG
        self._objects: dict[str, bytes] = {}
        self._large_objects: dict[str, _LargeObjectRecord] = {}
        self._upload_log: list[str] = []
        self._now_ms = now_ms
        self._unique_token = unique_token
        self._fault_mode = fault_mode
        # 允许测试注入更小的阈值，避免为覆盖大对象路径真的写 32MiB 文件。
        self._max_in_memory_object_bytes = max_in_memory_object_bytes

    # —— 测试辅助 ——

    def set_fault_mode(self, mode: str) -> None:
        self._fault_mode = mode

    @property
    def uploaded_keys(self) -> list[str]:
        """历史上传顺序：不随删除而消失，与当前存量（object_count）区分。"""
        return list(self._upload_log)

    @property
    def object_count(self) -> int:
        """当前存量：小对象（内容常驻）与大对象（仅元数据）合计。"""
        return len(self._objects) + len(self._large_objects)

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

    def _exists(self, object_key: str) -> bool:
        return object_key in self._objects or object_key in self._large_objects

    def _require_exists(self, object_key: str) -> None:
        if self._fault_mode == FaultMode.NOT_FOUND or not self._exists(object_key):
            raise StorageServerError(
                f"object not found: {object_key}",
                code="NoSuchKey",
                request_id="mock-request-id-0002",
                status_code=404,
            )

    def _require(self, object_key: str) -> bytes:
        """取小对象内容；大对象没有常驻内容，调用方需先判断再走各自的读取路径。"""
        self._require_exists(object_key)
        if object_key in self._large_objects:
            raise StorageClientError(
                f"本地 Mock 不保留大对象内容（object_key={object_key}），无法整体读取；"
                "如需处理内容请使用 download_to_file 取回本地文件"
            )
        return self._objects[object_key]

    # —— 契约实现 ——

    def upload_file(self, local_path: str | Path, object_key: str) -> StoredObject:
        self._maybe_fail("upload")
        path = Path(local_path)
        if not path.is_file():
            raise StorageClientError(f"本地文件不存在：{path}")
        size = path.stat().st_size
        if size > self._max_in_memory_object_bytes:
            # 大对象：只记录定位与校验所需的元数据，内容留在原地，不读入内存。
            self._large_objects[object_key] = _LargeObjectRecord(
                local_path=path.resolve(), size=size, digest=_sha256_file(path)
            )
            self._objects.pop(object_key, None)
        else:
            data = path.read_bytes()
            self._objects[object_key] = data
            self._large_objects.pop(object_key, None)
        self._upload_log.append(object_key)
        return StoredObject(object_key=object_key, size=size)

    def download_to_file(self, object_key: str, local_path: str | Path) -> Path:
        self._maybe_fail("download")
        self._require_exists(object_key)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if object_key in self._large_objects:
            record = self._large_objects[object_key]
            try:
                shutil.copyfile(record.local_path, target)
            except OSError as exc:
                # 大对象内容留在原路径上；源文件被删除或移动后无法再取回，
                # 统一收敛为领域异常，与其他失败路径保持一致。
                raise StorageClientError(
                    f"本地 Mock 的大对象源文件已不可用（object_key={object_key}，"
                    f"local_path={record.local_path}），无法取回内容：{exc}"
                ) from None
        else:
            target.write_bytes(self._objects[object_key])
        return target

    def get_object_bytes(self, object_key: str) -> bytes:
        self._maybe_fail("read")
        return self._require(object_key)

    def delete_object(self, object_key: str) -> None:
        self._maybe_fail("delete")
        self._objects.pop(object_key, None)
        self._large_objects.pop(object_key, None)

    def generate_presigned_url(self, object_key: str, expires_seconds: int | None = None) -> str:
        self._maybe_fail("presign")
        ttl = self.config.presigned_ttl_seconds if expires_seconds is None else int(expires_seconds)
        return (
            f"https://mock-tos.local/{self.config.bucket}/{object_key}"
            f"?X-Tos-Expires={ttl}&expires={ttl}"
        )

    def generate_object_key(self, kind: str, filename: str) -> str:
        return build_object_key(
            self.config.object_prefix,
            kind,
            filename,
            now_ms=self._now_ms,
            unique=self._unique_token,
        )
