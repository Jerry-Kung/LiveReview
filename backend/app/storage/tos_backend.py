"""火山引擎 TOS 对象存储实现：延迟导入 SDK，统一映射领域异常。

用法与封装要点参考 `docs/tos_examples/`。AK/SK 与预签名 URL 一律不进日志。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from app.storage.base import (
    ObjectStorage,
    StorageClientError,
    StorageConfig,
    StorageServerError,
    StoredObject,
    build_object_key,
)

logger = logging.getLogger(__name__)


def _import_tos() -> Any:
    """延迟导入官方 SDK；缺失时由调用处转换为 StorageClientError，便于本地无 SDK 场景。"""
    import tos

    return tos


class TosStorage(ObjectStorage):
    """基于官方 SDK 的实现；TosClientV2 线程安全，进程内复用同一实例。"""

    def __init__(self, config: StorageConfig) -> None:
        self.config = config
        self._client: Any | None = None
        self._lock = threading.Lock()

    def _safe_message(self, exc: BaseException) -> str:
        """提取异常文本并脱敏：SDK 报错可能原样回显请求参数，须剔除 AK/SK。"""
        message = str(getattr(exc, "message", None) or exc)
        for secret in (self.config.access_key, self.config.secret_key):
            if secret:
                message = message.replace(secret, "***")
        return message

    @property
    def client(self) -> Any:
        """首次访问时创建客户端，锁保护避免并发重复初始化。"""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._create_client()
        return self._client

    def _create_client(self) -> Any:
        try:
            tos = _import_tos()
        except ImportError as exc:
            raise StorageClientError(
                "未安装 TOS SDK，请先安装依赖（tos>=2.6.0）后再启用 TOS 存储"
            ) from exc

        try:
            return tos.TosClientV2(
                self.config.access_key,
                self.config.secret_key,
                self.config.endpoint,
                self.config.region,
            )
        except Exception as exc:  # SDK 初始化异常面较宽，统一收敛为领域异常
            if isinstance(exc, self._sdk_client_error()):
                raise StorageClientError(f"TOS 客户端初始化失败：{self._safe_message(exc)}") from exc
            if isinstance(exc, self._sdk_server_error()):
                raise self._as_server_error(exc) from exc
            raise StorageClientError(f"TOS 客户端初始化失败：{self._safe_message(exc)}") from exc

    def _sdk_client_error(self) -> tuple[type, ...]:
        return (getattr(_import_tos().exceptions, "TosClientError"),)

    def _sdk_server_error(self) -> tuple[type, ...]:
        return (getattr(_import_tos().exceptions, "TosServerError"),)

    def _as_server_error(self, exc: BaseException) -> StorageServerError:
        code = getattr(exc, "code", None)
        request_id = getattr(exc, "request_id", None)
        status_code = getattr(exc, "status_code", None)
        message = self._safe_message(exc)
        # request_id 是向火山引擎定位问题的关键信息
        logger.error(
            "TOS 服务端错误 bucket=%s code=%s status=%s request_id=%s message=%s",
            self.config.bucket,
            code,
            status_code,
            request_id,
            message,
        )
        return StorageServerError(
            message, code=code, request_id=request_id, status_code=status_code
        )

    def _as_client_error(self, exc: BaseException) -> StorageClientError:
        message = self._safe_message(exc)
        logger.error("TOS 客户端错误 endpoint=%s message=%s", self.config.endpoint, message)
        return StorageClientError(message)

    def _run(self, action: str, call: Any) -> Any:
        """统一执行 SDK 调用并映射异常，避免各方法重复 try/except 分支。"""
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 —— 需按 SDK 异常类型分流后重新抛出
            if isinstance(exc, self._sdk_client_error()):
                raise self._as_client_error(exc) from exc
            if isinstance(exc, self._sdk_server_error()):
                raise self._as_server_error(exc) from exc
            raise StorageClientError(f"{action} 失败：{self._safe_message(exc)}") from exc

    # —— 契约实现 ——

    def upload_file(self, local_path: str | Path, object_key: str) -> StoredObject:
        path = Path(local_path)
        if not path.is_file():
            raise StorageClientError(f"本地文件不存在：{path}")
        size = path.stat().st_size
        self._run(
            "上传对象",
            lambda: self.client.put_object_from_file(self.config.bucket, object_key, str(path)),
        )
        logger.info("已上传对象 bucket=%s key=%s size=%d", self.config.bucket, object_key, size)
        return StoredObject(object_key=object_key, size=size)

    def download_to_file(self, object_key: str, local_path: str | Path) -> Path:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            "下载对象",
            lambda: self.client.get_object_to_file(self.config.bucket, object_key, str(target)),
        )
        return target

    def get_object_bytes(self, object_key: str) -> bytes:
        def _call() -> bytes:
            resp = self.client.get_object(self.config.bucket, object_key)
            return resp.read()

        return self._run("读取对象", _call)

    def delete_object(self, object_key: str) -> None:
        self._run(
            "删除对象",
            lambda: self.client.delete_object(self.config.bucket, object_key),
        )
        logger.info("已删除对象 bucket=%s key=%s", self.config.bucket, object_key)

    def generate_presigned_url(self, object_key: str, expires_seconds: int | None = None) -> str:
        ttl = self.config.presigned_ttl_seconds if expires_seconds is None else int(expires_seconds)

        def _call() -> str:
            tos = _import_tos()
            # 官方签名：pre_signed_url(method, bucket, key, expires=...)
            resp = self.client.pre_signed_url(
                tos.HttpMethodType.Http_Method_Get,
                self.config.bucket,
                object_key,
                expires=ttl,
            )
            return resp.signed_url

        # 预签名 URL 本身即凭据，日志只记录对象键与有效期
        logger.info("已生成预签名 URL key=%s ttl=%ds", object_key, ttl)
        return self._run("生成预签名 URL", _call)

    def generate_object_key(self, kind: str, filename: str) -> str:
        return build_object_key(self.config.object_prefix, kind, filename)
