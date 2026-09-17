"""识别任务的提示词：默认任务 Prompt 与输出格式约束。

默认 Prompt 是本次验证的题面（「识别出视频中的所有人声语音」），由 `LLM_UNDERSTANDING_PROMPT`
覆盖。输出格式约束单独拼接而不是让使用者自己写：解析器认的是同一套字段，题面可以换、
契约不能换。
"""

from __future__ import annotations

# 默认任务 Prompt：本次验证使用的题面，可按 `LLM_UNDERSTANDING_PROMPT` 覆盖
DEFAULT_UNDERSTANDING_PROMPT = "请你识别出附件视频中的所有人声语音"

# 输出格式约束：与 `parsing.py` 的解析契约一致；改这里必须同步改解析器
OUTPUT_FORMAT_INSTRUCTION = (
    "以 JSON 格式输出开始时间（start_time）、结束时间（end_time）、语音内容（content）。"
    "start_time 与 end_time 用片段内的秒数表示，保留一位小数；"
    "content 保留原话表达，不要翻译、不要概括。"
    "只输出一个 JSON 数组，不要输出数组以外的任何文字。"
    "如果片段中没有任何人声语音，输出空数组 []。"
)


def build_prompt(task_prompt: str | None = None) -> str:
    """拼接最终的 Prompt：题面 + 固定的输出格式约束。

    题面为空时用默认题面，避免把空字符串当成「用户要求什么都不识别」。
    """
    prompt = (task_prompt or "").strip() or DEFAULT_UNDERSTANDING_PROMPT
    return f"{prompt}，{OUTPUT_FORMAT_INSTRUCTION}"
