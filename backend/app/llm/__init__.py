"""模型接入模块：契约、实现与提示词。

业务代码统一从本包导入 `get_understanding_client()`，不直接依赖模型厂商 SDK。
"""

from __future__ import annotations

import logging
import threading

from app.llm.base import (
    TERMINAL_UNDERSTANDING_STATUSES,
    UNDERSTANDING_SKIPPED,
    UNDERSTANDING_STATUS_FAILED,
    UNDERSTANDING_STATUS_PENDING,
    UNDERSTANDING_STATUS_RUNNING,
    UNDERSTANDING_STATUS_SUCCEEDED,
    LLMClientError,
    LLMConfig,
    LLMError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    SpeechSegment,
    UnderstandingClient,
    UnderstandingResult,
    describe_error,
)
from app.llm.client import OpenAIUnderstandingClient
from app.llm.parsing import AlignedSegment, align_segments, parse_segments
from app.llm.prompts import DEFAULT_UNDERSTANDING_PROMPT, build_prompt

logger = logging.getLogger(__name__)

_client: UnderstandingClient | None = None
_lock = threading.Lock()


def build_client(settings) -> UnderstandingClient:
    """按配置构造识别客户端；不缓存，供测试与自检命令直接调用。

    配置不完整时直接报错：视频理解任务一旦进入执行状态就会真发请求，缺配置应当在
    启动识别之前暴露，而不是让整场切片逐片失败。
    """
    missing = settings.llm_missing_fields
    if missing:
        raise LLMClientError(f"缺少模型配置：{'、'.join(missing)}")
    return OpenAIUnderstandingClient(settings.llm_config())


def get_understanding_client() -> UnderstandingClient:
    """返回进程内复用的识别客户端。

    默认按配置构造并缓存（底层 HTTP 客户端线程安全、连接池可复用）；测试与自检用
    `use_client()` 整体替换。客户端来源只有这一处，避免各调用点各自打补丁。
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                from app.config import get_settings

                _client = build_client(get_settings())
    return _client


def use_client(client: UnderstandingClient | None) -> None:
    """注册识别客户端实现；传 None 表示恢复为「按配置构造」。"""
    global _client
    with _lock:
        _client = client


def reset_client() -> None:
    """丢弃缓存的客户端，供测试与配置变更后重建使用。"""
    use_client(None)


__all__ = [
    "DEFAULT_UNDERSTANDING_PROMPT",
    "TERMINAL_UNDERSTANDING_STATUSES",
    "UNDERSTANDING_SKIPPED",
    "UNDERSTANDING_STATUS_FAILED",
    "UNDERSTANDING_STATUS_PENDING",
    "UNDERSTANDING_STATUS_RUNNING",
    "UNDERSTANDING_STATUS_SUCCEEDED",
    "AlignedSegment",
    "LLMClientError",
    "LLMConfig",
    "LLMError",
    "LLMResponseError",
    "LLMServerError",
    "LLMTimeoutError",
    "OpenAIUnderstandingClient",
    "SpeechSegment",
    "UnderstandingClient",
    "UnderstandingResult",
    "align_segments",
    "build_client",
    "build_prompt",
    "describe_error",
    "get_understanding_client",
    "parse_segments",
    "reset_client",
    "use_client",
]
