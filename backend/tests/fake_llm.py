"""识别与复盘链路的测试替身：假模型客户端与样例输出。

放在 `tests/` 下由 conftest 与各用例共用；真实模型调用绝不出现在自动化测试里——
两条链路要验证的是编排、解析、对齐、合并与失败隔离，这些都不需要真的花钱调模型。
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.base import LLMError, ReviewResult, UnderstandingResult
from app.llm.parsing import normalise_review, parse_response, parse_review_response

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


class _FakeChatResponse:
    """伪造 chat.completions 接口的返回对象：结构与 responses 不同，文本在 choices 里。"""

    def __init__(self, text: str, *, usage: dict | None = None) -> None:
        self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": text})()})()]
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


# —— 复盘链路（V0.3）——

# 一份覆盖全部字段的复盘结论：解析与合并的用例都以它为基准。
# 这里**不含片段覆盖数**（`clip_count` / `succeeded_clip_count` / `failed_clip_count`）：
# 那是程序按真实覆盖面算出来的，由 `run_review` 写进结论，模型不该也无法给出。
SAMPLE_REVIEW = {
    "one_line": "本场讲清了越野场景，但报价前的价值翻译不足，留资引导偏早。",
    "analysis_level": "部分",
    "level_reason": "有 1 片识别失败，相关时段内容缺失",
    "key_events": [
        {"start_seconds": 12.0, "end_seconds": 40.0, "topic": "A 产品讲解", "summary": "开场介绍三电"},
        {"start_seconds": 300.0, "end_seconds": 360.0, "topic": "F 权益政策", "summary": "公布购车权益"},
    ],
    "findings": [
        {
            "dimension": "产品讲解",
            "judgement": "参数直给偏多，缺少场景翻译",
            "evidence_level": "事实",
            "evidence": "电机功率 400 千瓦，扭矩 800 牛米",
            "start_seconds": 120.0,
            "suggestion": "先讲越野场景里的通过性，再给参数",
        }
    ],
    "top_issues": [
        {
            "problem": "权益公布前没有先建立价值",
            "evidence": "现在下订直接减两万",
            "impact": "留资前的信任建立环节",
            "root_cause": "话术方法",
            "action": "权益前先用 30 秒讲清三项核心价值",
        }
    ],
    "next_actions": [
        {
            "goal": "报价前完成价值翻译",
            "how": "报价前 3 分钟按场景讲三项配置",
            "observe": "是否在报价前完成场景翻译",
        }
    ],
    "missing_info": ["本场没有分钟级数据，无法判断停留与转化效果"],
}


class FakeReviewClient:
    """按脚本返回结果的假复盘客户端；语义与 `FakeUnderstandingClient` 一致。

    `calls` 记录每次调用的 system 与 user 提示词：用例据此断言「准则是否进了 system」
    与「送模型的是不是转写正文」。
    """

    def __init__(self, *, canned: str | None = None, script: list[Any] | None = None,
                 repeat_last: bool = False) -> None:
        self.canned = canned if canned is not None else review_output(SAMPLE_REVIEW)
        self.script: list[Any] = list(script or [])
        self.repeat_last = repeat_last
        self.calls: list[dict[str, str]] = []
        self._queue: list[Any] = list(self.script)

    def review(self, *, system_prompt: str, user_prompt: str) -> ReviewResult:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        if self._queue:
            if len(self._queue) == 1 and self.repeat_last:
                item = self._queue[0]
            else:
                item = self._queue.pop(0)
        else:
            item = self.canned
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ReviewResult):
            result = item
        elif isinstance(item, dict):
            payload, warnings = normalise_review(item)
            result = ReviewResult(payload=payload, raw_text=json.dumps(item, ensure_ascii=False),
                                  usage={"total_tokens": 7}, warnings=warnings)
        else:
            result = parse_review_response(_FakeChatResponse(str(item), usage={"total_tokens": 7}))
        result.attempts = max(1, result.attempts)
        return result


def review_output(payload: dict) -> str:
    """把复盘结论序列化成模型输出的样子。"""
    return json.dumps(payload, ensure_ascii=False)


class BoomError(LLMError):
    """测试用异常：模拟模型侧的失败。"""
