"""对象存储模块：契约、TOS 实现、本地 Mock 实现与工厂。

业务代码统一从本包导入 `ObjectStorage` 与 `get_storage()`，不直接依赖供应商 SDK。
"""

from __future__ import annotations

import logging
import threading

from app.storage.base import (
    KNOWN_KINDS,
    OBJECT_KIND_CLIP,
    OBJECT_KIND_ORIGINAL,
    ObjectStorage,
    StorageClientError,
    StorageConfig,
    StorageError,
    StorageServerError,
    StoredObject,
    build_object_key,
)
from app.storage.mock import InMemoryStorage

logger = logging.getLogger(__name__)

BACKEND_AUTO = "auto"
BACKEND_TOS = "tos"
BACKEND_MOCK = "mock"
KNOWN_BACKENDS: frozenset[str] = frozenset({BACKEND_AUTO, BACKEND_TOS, BACKEND_MOCK})

_storage: ObjectStorage | None = None
_lock = threading.Lock()


def build_storage(settings) -> ObjectStorage:
    """按配置构造存储实现；不缓存，供测试与自检命令直接调用。"""
    if settings.storage_backend not in KNOWN_BACKENDS:
        raise StorageClientError(
            f"未知的 STORAGE_BACKEND：{settings.storage_backend}；可用值：{sorted(KNOWN_BACKENDS)}"
        )

    if settings.storage_backend == BACKEND_MOCK:
        return InMemoryStorage(settings.storage_config())

    if not settings.storage_configured:
        missing = "、".join(settings.storage_missing_fields)
        if settings.storage_backend == BACKEND_TOS:
            raise StorageClientError(f"STORAGE_BACKEND=tos 但缺少必填配置：{missing}")
        logger.warning(
            "未配置 TOS 凭据（缺少 %s），对象存储回落为本地 Mock 实现；真实上传需在测试环境配置凭据",
            missing,
        )
        return InMemoryStorage(settings.storage_config())

    from app.storage.tos_backend import TosStorage

    return TosStorage(settings.storage_config())


def get_storage() -> ObjectStorage:
    """进程内单例：TosClientV2 线程安全，复用同一实例。"""
    global _storage
    if _storage is None:
        with _lock:
            if _storage is None:
                from app.config import get_settings

                _storage = build_storage(get_settings())
    return _storage


def reset_storage() -> None:
    """清空单例，供测试与配置变更后重建使用。"""
    global _storage
    with _lock:
        _storage = None


__all__ = [
    "BACKEND_AUTO",
    "BACKEND_MOCK",
    "BACKEND_TOS",
    "KNOWN_BACKENDS",
    "KNOWN_KINDS",
    "OBJECT_KIND_CLIP",
    "OBJECT_KIND_ORIGINAL",
    "InMemoryStorage",
    "ObjectStorage",
    "StorageClientError",
    "StorageConfig",
    "StorageError",
    "StorageServerError",
    "StoredObject",
    "build_object_key",
    "build_storage",
    "get_storage",
    "reset_storage",
]
