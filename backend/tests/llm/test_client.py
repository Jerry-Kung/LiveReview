"""模型客户端的测试：用假 SDK 覆盖重试、超时与请求构造，不触网。

这里验证的是「怎么调」而不是「模型答得准不准」：请求体形态、超时与重试边界、
SDK 异常到领域异常的映射。真实的识别质量由测试环境的人工核查覆盖。
"""

from __future__ import annotations

import sys
import types

import pytest

from app.llm.base import LLMClientError, LLMConfig, LLMServerError, LLMTimeoutError
from app.llm.client import OpenAIUnderstandingClient
from tests.fake_llm import SAMPLE_JSON


# —— 假 SDK ——


class _FakeError(Exception):
    def __init__(self, message, *, status_code=None, request_id=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.body = body


class _FakeAPITimeoutError(_FakeError):
    pass


class _FakeAPIConnectionError(_FakeError):
    pass


class _FakeAPIStatusError(_FakeError):
    pass


class _FakeAPIError(_FakeError):
    pass


class _FakeResponse:
    def __init__(self, text: str = SAMPLE_JSON, *, status: str = "completed", usage=None, error=None):
        self.status = status
        self.error = error
        self.output = [
            type(
                "Item",
                (),
                {"content": [type("Block", (), {"type": "output_text", "text": text})()]},
            )()
        ]
        self.usage = usage


class _FakeResponses:
    def __init__(self, script: list):
        self._script = list(script)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self._script.pop(0) if self._script else _FakeResponse()
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.responses = _FakeResponses(_SCRIPT)


_SCRIPT: list = []
_CLIENTS: list[_FakeClient] = []


def _install_fake_openai(monkeypatch, script: list) -> None:
    """把假 SDK 装进 sys.modules：客户端内部 `import openai` 时拿到它。"""
    global _SCRIPT
    _SCRIPT = list(script)
    _CLIENTS.clear()

    module = types.ModuleType("openai")
    module.APITimeoutError = _FakeAPITimeoutError
    module.APIConnectionError = _FakeAPIConnectionError
    module.APIStatusError = _FakeAPIStatusError
    module.APIError = _FakeAPIError
    module.OpenAI = _make_client_class()
    monkeypatch.setitem(sys.modules, "openai", module)


def _make_client_class():
    class _Client(_FakeClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            _CLIENTS.append(self)

    return _Client


def _config(**overrides) -> LLMConfig:
    base = {
        "base_url": "https://llm.test/api/v3",
        "api_key": "sk-test",
        "model_name": "doubao-seed-test",
        "timeout_seconds": 900,
        "fps": 1.0,
        "max_attempts": 3,
    }
    base.update(overrides)
    return LLMConfig(**base)


def _client(script: list, monkeypatch, **overrides) -> OpenAIUnderstandingClient:
    _install_fake_openai(monkeypatch, script)
    return OpenAIUnderstandingClient(_config(**overrides), sleep=lambda _seconds: None)


# —— 请求构造 ——


def test_request_carries_video_url_prompt_and_fps(monkeypatch):
    """请求体要与示例代码一致：input_video + input_text，fps 由配置给出。"""
    client = _client([_FakeResponse()], monkeypatch, fps=2.0)
    client.understand(video_url="https://tos.test/clip.mp4?sig=abc", prompt="识别语音")

    request = _CLIENTS[0].responses.requests[0]
    assert request["model"] == "doubao-seed-test"
    content = request["input"][0]["content"]
    assert content[0] == {"type": "input_video", "video_url": "https://tos.test/clip.mp4?sig=abc", "fps": 2.0}
    assert content[1] == {"type": "input_text", "text": "识别语音"}


def test_client_is_created_once_and_disables_sdk_retries(monkeypatch):
    """底层客户端只建一次，且关掉 SDK 内建重试——重试由本模块统一控制。"""
    client = _client([_FakeResponse(), _FakeResponse()], monkeypatch)
    client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    client.understand(video_url="https://tos.test/b.mp4", prompt="p")

    assert len(_CLIENTS) == 1
    assert _CLIENTS[0].kwargs["max_retries"] == 0
    assert _CLIENTS[0].kwargs["timeout"] == 900
    assert _CLIENTS[0].kwargs["base_url"] == "https://llm.test/api/v3"


def test_attempts_reflect_actual_request_count(monkeypatch):
    """成功返回时带上实际请求次数，便于判断重试是否被触发过。"""
    client = _client([_FakeResponse()], monkeypatch)
    result = client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    assert result.attempts == 1
    assert len(result.segments) == 2


# —— 重试边界 ——


def test_retries_transient_failure_then_succeeds(monkeypatch):
    """可重试错误退避后再试一次，最终成功。"""
    error = _FakeAPIStatusError("rate limited", status_code=429, request_id="req-1")
    client = _client([error, _FakeResponse()], monkeypatch, max_attempts=2)

    result = client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    assert result.attempts == 2
    assert len(_CLIENTS[0].responses.requests) == 2


def test_raises_after_exhausting_attempts(monkeypatch):
    """重试次数用尽后抛出最后一次的服务端错误，保留 request_id。"""
    error = _FakeAPIStatusError("busy", status_code=503, request_id="req-9")
    client = _client([error, error], monkeypatch, max_attempts=2)

    with pytest.raises(LLMServerError) as excinfo:
        client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    assert excinfo.value.request_id == "req-9"
    assert len(_CLIENTS[0].responses.requests) == 2


def test_client_error_is_not_retried(monkeypatch):
    """请求本身的问题重试无用：4xx 直接失败，不浪费配额。"""
    error = _FakeAPIStatusError("bad request", status_code=400, body={"error": {"code": "InvalidParameter"}})
    client = _client([error, _FakeResponse()], monkeypatch, max_attempts=3)

    with pytest.raises(LLMClientError):
        client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    # 只发了一次请求
    assert len(_CLIENTS[0].responses.requests) == 1


def test_timeout_is_mapped_and_retried(monkeypatch):
    """超时映射为独立异常类型并按可重试处理。"""
    client = _client([_FakeAPITimeoutError("timed out"), _FakeResponse()], monkeypatch, max_attempts=2)

    result = client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    assert result.attempts == 2


def test_timeout_exhausted_raises_timeout_error(monkeypatch):
    client = _client([_FakeAPITimeoutError("timed out")], monkeypatch, max_attempts=1)
    with pytest.raises(LLMTimeoutError):
        client.understand(video_url="https://tos.test/a.mp4", prompt="p")


def test_connection_error_is_client_error(monkeypatch):
    client = _client([_FakeAPIConnectionError("no route")], monkeypatch)
    with pytest.raises(LLMClientError):
        client.understand(video_url="https://tos.test/a.mp4", prompt="p")


# —— 输出与状态 ——


def test_incomplete_response_is_treated_as_server_error(monkeypatch):
    """生成被中断不能当成「这个片段没人说话」。"""
    incomplete = _FakeResponse("", status="incomplete")
    client = _client([incomplete], monkeypatch, max_attempts=1)

    with pytest.raises(LLMServerError) as excinfo:
        client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    assert "incomplete" in str(excinfo.value)


def test_unparseable_output_is_treated_as_server_error(monkeypatch):
    client = _client([_FakeResponse("这段视频里有很多人在讲话")], monkeypatch, max_attempts=1)
    with pytest.raises(LLMServerError):
        client.understand(video_url="https://tos.test/a.mp4", prompt="p")


def test_empty_array_output_succeeds(monkeypatch):
    """空数组是有效结果：模型明确说这个片段没有人声。"""
    client = _client([_FakeResponse("[]")], monkeypatch)
    result = client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    assert result.segments == []


def test_usage_is_carried_through(monkeypatch):
    usage = type("Usage", (), {"model_dump": lambda self: {"total_tokens": 321}})()
    client = _client([_FakeResponse(usage=usage)], monkeypatch)
    result = client.understand(video_url="https://tos.test/a.mp4", prompt="p")
    assert result.usage == {"total_tokens": 321}
