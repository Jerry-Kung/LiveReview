"""模型客户端实现：基于 OpenAI 兼容的 `responses` 接口做视频理解。

接入形态取自 `python_examples.py`：把片段的预签名 URL 与提示词一起发给模型，模型直接
「看 + 听」片段并返回带时间戳的人声语音记录。

三个实现要点：

- **超时留足**：单次视频理解是分钟级任务，默认 900 秒。超时是预期内失败，单独映射为
  `LLMTimeoutError`，由任务体按「可重试」处理。
- **超时只在客户端侧**：`max_retries=0` 关掉 SDK 内建重试，重试策略集中在本模块，
  否则「用了几次请求、共耗时多久」在任务记录里对不上。
- **凭据不外泄**：视频 URL 里带签名，等于凭据；日志与失败原因只记录对象键与时长。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from app.llm.base import (
    LLMClientError,
    LLMConfig,
    LLMServerError,
    LLMTimeoutError,
    UnderstandingResult,
)
from app.llm.parsing import parse_response

logger = logging.getLogger(__name__)

# 瞬时故障的退避基数（秒）：第 n 次重试等待 _BACKOFF_BASE * 2**(n-1)
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_MAX_SECONDS = 30.0

# 需要退避后重试的 HTTP 状态：限流与 5xx
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def _import_openai() -> Any:
    """延迟导入 SDK；缺失时由调用处转换成 LLMClientError，便于无 SDK 环境跑测试。"""
    import openai

    return openai


def _error_meta(exc: BaseException) -> tuple[int | None, str | None, str | None]:
    """从 SDK 异常里取出状态码、错误码与 request_id；取不到就是 None。"""
    status_code = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    code: str | None = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
        elif isinstance(body.get("code"), str):
            code = body["code"]
    return status_code, code, request_id


def _response_error_text(response: Any) -> str:
    """响应里的 error 字段转文本；空响应也要给出可读原因。"""
    error = getattr(response, "error", None)
    if error is None:
        return "响应中没有 error 字段"
    message = getattr(error, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return str(error)


class OpenAIUnderstandingClient:
    """一次进程内复用的识别客户端；底层 HTTP 客户端线程安全。"""

    def __init__(self, config: LLMConfig, *, sleep: Callable[[float], None] | None = None) -> None:
        self.config = config
        # 可注入的等待函数：测试不必真的睡满退避时间
        self._sleep = sleep or time.sleep
        self._client: Any | None = None
        self._lock = threading.Lock()

    @property
    def client(self) -> Any:
        """懒构造底层客户端，锁保护避免并发重复初始化。"""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._create_client()
        return self._client

    def _create_client(self) -> Any:
        try:
            openai = _import_openai()
        except ImportError as exc:  # pragma: no cover —— 依赖缺失属部署问题
            raise LLMClientError(
                "未安装模型 SDK，请先安装依赖（openai>=1.40）后再启用模型识别"
            ) from exc
        return openai.OpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.timeout_seconds,
            # 重试集中在 understand()，SDK 侧不再自行重试
            max_retries=0,
        )

    # —— 契约实现 ——

    def understand(self, *, video_url: str, prompt: str) -> UnderstandingResult:
        """对一段视频执行一次识别。

        重试边界按异常语义划分：`LLMClientError` 是请求本身的问题（配置错、被拒绝），
        重试多少次都一样，直接抛出；超时、服务端错误与输出无法解析都可能是瞬时故障，
        按配置退避重试。
        """
        attempts = max(1, self.config.max_attempts)
        last_error: BaseException | None = None

        for attempt in range(1, attempts + 1):
            try:
                result = self._call_once(video_url=video_url, prompt=prompt)
            except LLMClientError:
                raise
            except (LLMTimeoutError, LLMServerError) as exc:
                last_error = exc
            else:
                result.attempts = attempt
                return result

            if attempt < attempts:
                delay = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_MAX_SECONDS)
                logger.warning(
                    "识别第 %d 次尝试失败（%s），%.1fs 后重试",
                    attempt,
                    type(last_error).__name__,
                    delay,
                )
                self._sleep(delay)

        assert last_error is not None
        raise last_error

    def _call_once(self, *, video_url: str, prompt: str) -> UnderstandingResult:
        openai = _import_openai()
        try:
            response = self.client.responses.create(
                model=self.config.model_name,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_video", "video_url": video_url, "fps": self.config.fps},
                            {"type": "input_text", "text": prompt},
                        ],
                    }
                ],
            )
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError(
                f"识别请求超时（{self.config.timeout_seconds}s）"
            ) from exc
        except openai.APIConnectionError as exc:
            raise LLMClientError(f"无法连接模型服务：{exc}") from exc
        except openai.APIStatusError as exc:
            status_code, code, request_id = _error_meta(exc)
            # 4xx 中除限流外都是请求本身的问题，重试无用
            if status_code is None or status_code not in _RETRYABLE_STATUS_CODES:
                raise LLMClientError(
                    f"模型服务拒绝了本次请求：status_code={status_code}，code={code}"
                ) from exc
            raise LLMServerError(
                "模型服务返回可重试错误",
                status_code=status_code,
                code=code,
                request_id=request_id,
            ) from exc
        except Exception as exc:  # noqa: BLE001 —— SDK 异常面较宽，统一收敛为领域异常
            raise LLMClientError(f"模型调用失败：{exc}") from exc

        status = getattr(response, "status", None)
        if status is not None and status != "completed":
            # 调用成功但生成被截断或中断：当作可重试的服务端问题，不能落库成空识别结果
            raise LLMServerError(
                f"模型未完成生成：status={status}，{_response_error_text(response)}"
            )

        try:
            return parse_response(response)
        except ValueError as exc:
            raise LLMServerError(f"模型输出无法解析：{exc}") from exc
