"""模型接入模块：契约与领域异常。

业务代码（任务体、路由层）只依赖本模块导出的类型与客户端工厂，不直接接触模型厂商
SDK——更换厂商或补充 ASR 等能力时，改动限制在本包内。

与存储模块的分层方式一致：`base.py` 放契约，`client.py` 放实现，`prompts.py` 放提示词，
`parsing.py` 放模型输出的解析与对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# 识别状态常量的权威定义在 `app/models.py`（与切片状态同处），这里只做转发，
# 使本包的使用方不必同时 import 两个模块。
from app.models import (
    REVIEW_SKIPPED,
    REVIEW_STATUS_FAILED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_RUNNING,
    REVIEW_STATUS_SUCCEEDED,
    TERMINAL_REVIEW_STATUSES,
    TERMINAL_UNDERSTANDING_STATUSES,
    UNDERSTANDING_SKIPPED,
    UNDERSTANDING_STATUS_FAILED,
    UNDERSTANDING_STATUS_PENDING,
    UNDERSTANDING_STATUS_RUNNING,
    UNDERSTANDING_STATUS_SUCCEEDED,
)


class LLMError(Exception):
    """模型接入模块所有可预期失败的基类。"""


class LLMClientError(LLMError):
    """调用侧问题：配置缺失、网络不通、请求被拒绝等。"""


class LLMServerError(LLMError):
    """模型服务端错误；保留状态码与服务端 request_id 供定位问题。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class LLMTimeoutError(LLMError):
    """单次请求超时：视频理解耗时较长，超时是预期内的失败，需要单独识别以便重试。"""


class LLMResponseError(LLMError):
    """模型返回了无法当作识别结果使用的内容（空输出、非 JSON、字段缺失）。"""


@dataclass(frozen=True)
class LLMConfig:
    """模型接入所需的全部配置，由 Settings 转换而来，不持有全局状态。"""

    base_url: str
    api_key: str
    model_name: str
    timeout_seconds: int = 900
    fps: float = 1.0
    max_attempts: int = 3
    # 复盘调用（V0.3）：纯文本分析，超时按文本生成的量级给，与视频理解分开配置
    review_timeout_seconds: int = 300
    review_max_attempts: int = 3


@dataclass(frozen=True)
class SpeechSegment:
    """一条人声语音：时间为**片段内的相对秒数**，对齐到原视频时间轴是调用方的事。

    `tone` 是 V0.3 起模型对语气与情绪的主观估计，可空——缺失只意味着这条记录少一类线索，
    不影响它作为语音证据本身的价值。
    """

    start_seconds: float
    end_seconds: float
    content: str
    tone: str = ""


@dataclass
class UnderstandingResult:
    """一次片段识别的产物：解析出的语音记录、token 用量与解析告警。"""

    segments: list[SpeechSegment] = field(default_factory=list)
    raw_text: str = ""
    usage: dict[str, Any] | None = None
    # 解析过程中的可核查告警（越界时间、被丢弃的条目等），随结果一并入库
    warnings: list[str] = field(default_factory=list)
    # 本次实际发出的请求次数（含退避重试）：重试是否被触发过是排查「模型不稳」的线索
    attempts: int = 1


class UnderstandingClient(Protocol):
    """识别客户端契约：任务体只依赖它，测试用假实现替换。"""

    def understand(self, *, video_url: str, prompt: str) -> UnderstandingResult:
        """对一段视频 URL 执行一次识别，返回解析后的语音记录。"""


@dataclass
class ReviewResult:
    """一次复盘调用的产物：结构化结论 + 原始返回 + 用量。

    `payload` 是**解析并归一后**的结论（字段齐备、缺失项已补默认值），`raw_text` 是模型
    原样返回的文本。两者都留档：前者供界面渲染，后者用于「模型到底答了什么」的复核与
    提示词调优——只留解析后的结果会让格式问题无从复现。
    """

    payload: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    usage: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    attempts: int = 1


class ReviewClient(Protocol):
    """复盘客户端契约：只做**纯文本**分析，输入是拼好的转写全文。"""

    def review(self, *, system_prompt: str, user_prompt: str) -> ReviewResult:
        """对一段转写文本执行一次复盘分析，返回结构化结论。"""


def describe_error(exc: BaseException) -> str:
    """把模型异常转换为可入库的诊断文本。

    服务端错误的 `request_id` 是向服务商定位问题的唯一凭据，必须随失败原因一起保留；
    只记录标识与消息，不含凭据与预签名 URL——视频 URL 本身即凭据，一律不回显。
    """
    if isinstance(exc, LLMServerError):
        parts = [exc.message]
        if exc.code:
            parts.append(f"code={exc.code}")
        if exc.status_code is not None:
            parts.append(f"status_code={exc.status_code}")
        if exc.request_id:
            parts.append(f"request_id={exc.request_id}")
        return "，".join(parts)
    return str(exc)
