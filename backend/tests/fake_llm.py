"""识别链路的测试替身：假模型客户端与样例输出。

放在 `tests/` 下由 conftest 与各用例共用；真实模型调用绝不出现在自动化测试里——
识别链路要验证的是编排、解析、对齐与失败隔离，这些都不需要真的花钱调模型。
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.base import LLMError, UnderstandingResult
from app.llm.parsing import parse_response

# 一段覆盖常见形态的模型输出：正常条目、带说明文字的数组、代码围栏包裹
SAMPLE_JSON = (
    '[{"start_time": 0.5, "end_time": 3.2, "content": "欢迎来到直播间"},'
    ' {"start_time": 4.0, "end_time": 7.5, "content": "今天这款到手价 199 元"}]'
)
FENCED_JSON = f"```json\n{SAMPLE_JSON}\n```"
EXPLAINED_JSON = f"识别结果如下：\n{SAMPLE_JSON}\n以上为全部人声。"


class _FakeResponse:
    """伪造 responses 接口的返回对象：只保留解析器真正读取的字段。"""

    def __init__(self, text: str, *, status: str = "completed", usage: dict | None = None) -> None:
        self.status = status
        self.output = [
            type("Item", (), {"content": [type("Block", (), {"type": "output_text", "text": text})()]})()
        ]
        self.usage = usage


class FakeUnderstandingClient:
    """按脚本返回结果的假客户端。

    脚本是**实例**属性，用例直接在自己的夹具里改它，不依赖类属性与夹具执行顺序：
    `script` 里的每一项要么是「模型输出文本」，要么是要抛出的异常，用完后回落到
    `canned`；`repeat_last` 为真时最后一项会被重复使用（用于「所有片段都失败」这类
    场景，不必按片段数把脚本写满）。

    每次调用的 `video_url` 与 `prompt` 都被记录下来，供断言「URL 是不是现签的」
    与「提示词是不是拼好的」。
    """

    def __init__(self, *, canned: str = SAMPLE_JSON, script: list[Any] | None = None,
                 repeat_last: bool = False) -> None:
        self.canned = canned
        self.script: list[Any] = list(script or [])
        self.repeat_last = repeat_last
        self.calls: list[dict[str, str]] = []
        self._queue: list[Any] = list(self.script)

    def understand(self, *, video_url: str, prompt: str) -> UnderstandingResult:
        self.calls.append({"video_url": video_url, "prompt": prompt})
        if self._queue:
            if len(self._queue) == 1 and self.repeat_last:
                item = self._queue[0]
            else:
                item = self._queue.pop(0)
        else:
            item = self.canned
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, UnderstandingResult):
            result = item
        elif item is None:
            result = UnderstandingResult(segments=[], raw_text="[]", usage={"total_tokens": 1})
        else:
            result = parse_response(_FakeResponse(str(item), usage={"total_tokens": 42}))
        result.attempts = max(1, result.attempts)
        return result



def json_output(segments: list[dict]) -> str:
    """把条目列表序列化成模型输出的样子。"""
    return json.dumps(segments, ensure_ascii=False)


class BoomError(LLMError):
    """测试用异常：模拟模型侧的失败。"""
