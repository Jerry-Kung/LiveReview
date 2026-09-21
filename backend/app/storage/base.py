"""对象存储契约与领域异常：业务层只依赖本模块，不接触供应商 SDK。"""

from __future__ import annotations

import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

# 对象用途分层：V0.1.3 上传原始视频，V0.1.5 生成切片
OBJECT_KIND_ORIGINAL = "original"
OBJECT_KIND_CLIP = "clip"
KNOWN_KINDS: frozenset[str] = frozenset({OBJECT_KIND_ORIGINAL, OBJECT_KIND_CLIP})

# 文件名主体只保留可安全用于对象键的字符（TOS 对象键不建议出现空格与特殊符号）
_UNSAFE_STEM_CHARS = re.compile(r"[^0-9A-Za-z._一-鿿-]+")
_FALLBACK_STEM = "object"
# 毫秒时间戳 + 8 位随机串，确保同毫秒内并发生成也不重名
_UNIQUE_LENGTH = 8


class StorageError(Exception):
    """存储模块所有可预期失败的基类。"""


class StorageClientError(StorageError):
    """调用侧问题：参数非法、网络/连接失败、凭据缺失等。"""


class StorageServerError(StorageError):
    """对象存储返回的服务端错误，request_id 用于向服务商定位问题。"""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.request_id = request_id
        self.status_code = status_code


@dataclass(frozen=True)
class StorageConfig:
    """存储后端所需的全部配置，由 Settings 转换而来，不持有全局状态。"""

    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    object_prefix: str = "liverreview/"
    presigned_ttl_seconds: int = 3600


@dataclass(frozen=True)
class StoredObject:
    """一次上传的结果：定位对象所需的最小信息。"""

    object_key: str
    size: int
    content_type: str | None = None


def _normalise_prefix(prefix: str) -> str:
    prefix = (prefix or "").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    stem = _UNSAFE_STEM_CHARS.sub("-", stem).strip("-.")
    return stem or _FALLBACK_STEM


def build_object_key(
    prefix: str,
    kind: str,
    filename: str,
    *,
    now_ms: int | None = None,
    unique: str | None = None,
) -> str:
    """生成 `{前缀}{kind}/{文件名主体}_{毫秒时间戳}_{唯一串}{扩展名}` 形式的对象键。

    now_ms 与 unique 仅供测试注入，生产调用使用当前时间与随机串。
    """
    if kind not in KNOWN_KINDS:
        raise ValueError(f"未知的对象用途 kind：{kind}；可用值：{sorted(KNOWN_KINDS)}")

    ext = Path(filename).suffix
    timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    token = unique if unique is not None else uuid.uuid4().hex[:_UNIQUE_LENGTH]
    return f"{_normalise_prefix(prefix)}{kind}/{_safe_stem(filename)}_{timestamp}_{token}{ext}"


def describe_error(exc: BaseException) -> str:
    """把存储异常转换为可入库的诊断文本。

    服务端错误的 `request_id` 是向服务商定位问题的唯一凭据，必须随失败原因一起保留；
    只记录标识与消息，不含凭据与预签名 URL。
    """
    if isinstance(exc, StorageServerError):
        parts = [exc.message]
        if exc.code:
            parts.append(f"code={exc.code}")
        if exc.request_id:
            parts.append(f"request_id={exc.request_id}")
        if exc.status_code is not None:
            parts.append(f"status_code={exc.status_code}")
        return "，".join(parts)
    return str(exc)


class ObjectStorage(ABC):
    """对象存储契约：上传、下载、读取、删除、预签名与对象键生成。"""

    @abstractmethod
    def upload_file(self, local_path: str | Path, object_key: str) -> StoredObject:
        """上传本地文件到指定对象键，返回可入库的对象信息。"""

    @abstractmethod
    def download_to_file(self, object_key: str, local_path: str | Path) -> Path:
        """把对象下载到本地路径（父目录自动创建），返回本地路径。"""

    @abstractmethod
    def get_object_bytes(self, object_key: str) -> bytes:
        """读取对象全部内容，用于小对象与连通自检。"""

    @abstractmethod
    def delete_object(self, object_key: str) -> None:
        """删除对象；对象不存在时视为成功（幂等）。"""

    @abstractmethod
    def generate_presigned_url(self, object_key: str, expires_seconds: int | None = None) -> str:
        """生成带有效期的下载链接。"""

    @abstractmethod
    def generate_object_key(self, kind: str, filename: str) -> str:
        """按用途与原始文件名生成唯一对象键。"""
