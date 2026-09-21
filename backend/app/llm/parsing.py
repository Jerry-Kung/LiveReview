"""模型输出的解析与时间对齐：把「模型返回的文本」变成「可核查的语音记录」。

模型（尤其是带音频的视频理解模型）输出格式并不稳定：常见形态有纯 JSON 数组、被
```json 包裹的数组、带一句说明再给数组、以及把数组包在 `{"segments": [...]}` 里。
解析器的职责是把这些形态都收敛成同一份结构，**并如实记录丢弃了什么**——识别缺口必须
显式可查，不能悄悄吞掉不合规的条目，否则用户看到的完整度是假的。

时间对齐：模型看到的是片段，返回的时间是片段内相对秒数；本系统的证据链以原视频时间轴
为准，因此每条记录都会被加上片段起点。越界时段只标记、不裁切——裁切会伪造出模型没有
给出的证据。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.llm.base import ReviewResult, SpeechSegment, UnderstandingResult

logger = logging.getLogger(__name__)

# 代码围栏：```json ... ``` / ``` ... ```
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*|\s*```$")

# 条目别名字典：模型可能用不同键名表达同一含义
_START_KEYS = ("start_time", "start", "start_seconds", "begin_time", "from")
_END_KEYS = ("end_time", "end", "end_seconds", "finish_time", "to")
_CONTENT_KEYS = ("content", "text", "speech", "sentence", "utterance")
# 语气与情绪（V0.3）：主观估计，模型可能不返回或写成别的键名；缺失不影响条目入库
_TONE_KEYS = ("tone", "emotion", "emotion_tone", "语气", "情绪")

# 容器键：数组被包在某个对象里时的候选键名
_ARRAY_KEYS = ("segments", "data", "result", "results", "items", "list", "speech", "utterances")

# 时间单位判定：模型偶尔按毫秒给时间。判定基准不是固定阈值，而是片段实际时长——
# 「1500」是 1500 秒还是 1500 毫秒，只有对照片段本身才知道；用固定阈值会把一个
# 几十分钟的长片段里的正常取值误判成毫秒。
_MILLISECOND_FACTOR = 1000.0
# 超出片段长度多少倍才认为单位不符：留出 2 倍余量，避免把模型的少量越界当成单位问题
_UNIT_SUSPICION_RATIO = 2.0

# 单条记录的时间容差（秒）：允许 end 略小于 start（模型常把相邻两句的边界写反），
# 超出容差才判为无效条目
_NEGATIVE_DURATION_TOLERANCE = 0.05


@dataclass(frozen=True)
class AlignedSegment:
    """对齐到原视频时间轴的一条语音记录。"""

    start_seconds: float
    end_seconds: float
    content: str
    # 语气与情绪的主观估计；模型没给就是空串
    tone: str = ""
    # 片段内的相对时间，保留下来便于对照原始模型输出定位偏差
    clip_start_seconds: float = 0.0
    clip_end_seconds: float = 0.0
    # 时间是否越出片段范围：只标记，不裁切
    out_of_range: bool = False


def extract_text(response: Any) -> str:
    """从 responses 接口的返回对象里取出全部文本输出。

    只认 `output_text` 类型的内容块；其它块（推理、工具调用等）不是识别结果，直接忽略。
    """
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        for block in getattr(item, "content", None) or []:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)
            if block_type == "output_text" and isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def extract_chat_text(response: Any) -> str:
    """从 chat.completions 接口的返回对象里取出文本。

    返回结构是 `choices[0].message.content`。空 choices（服务端异常时可能出现）按空文本
    处理，由调用方判为「无法解析」——那属于失败，不能当成空结论落库。
    """
    choices = getattr(response, "choices", None) or []
    parts: list[str] = []
    for choice in choices:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts).strip()


def strip_code_fence(text: str) -> str:
    """剥掉整段被代码围栏包裹的外壳；没有围栏时原样返回。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # 只处理「开头有围栏」的情况：去掉首行围栏与结尾围栏
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _iter_json_candidates(text: str) -> list[str]:
    """按「最可能成功」的顺序给出候选 JSON 片段。"""
    candidates: list[str] = []
    stripped = strip_code_fence(text)
    if stripped:
        candidates.append(stripped)

    # 从第一个 '[' 到最后一个 ']'：容忍数组前后夹带的说明文字
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    # 兜底：从第一个 '{' 到最后一个 '}'，覆盖「对象包数组」的形态
    obj_start = stripped.find("{")
    obj_end = stripped.rfind("}")
    if obj_start != -1 and obj_end > obj_start:
        candidates.append(stripped[obj_start : obj_end + 1])
    return candidates


def loads_segments(text: str) -> list[Any] | None:
    """把模型输出解析成条目列表；完全解析不出来时返回 None。

    返回 None 与返回空列表含义不同：前者是「模型没说清楚」，后者是「模型说这里没人说话」。
    调用方据此决定是判失败还是记一条空结果。
    """
    for candidate in _iter_json_candidates(text):
        try:
            payload = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        items = _as_items(payload)
        if items is not None:
            return items
    return None


def _as_items(payload: Any) -> list[Any] | None:
    """从解析出的 JSON 里找出条目列表；结构不认识时返回 None。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _ARRAY_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # 单个条目被直接返回（只识别到一句话时常见）
        if _pick(payload, _START_KEYS) is not None and _pick(payload, _CONTENT_KEYS) is not None:
            return [payload]
    return None


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按候选键名取值；键名大小写不敏感，值为空串/None 视为未提供。"""
    lowered = {str(key).strip().lower(): value for key, value in item.items()}
    for key in keys:
        if key in lowered:
            value = lowered[key]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None


def _to_seconds(value: Any) -> float | None:
    """把时间取值转成秒；字符串里带数字时按数字提取（如 "12.5s"、"00:12.5"）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match is None:
            return None
        seconds = float(match.group())
        # 形如 00:12.5 / 01:02:03 的时分秒写法：按冒号分段换算
        if ":" in text:
            parts = [float(part) for part in re.findall(r"\d+(?:\.\d+)?", text)]
            if len(parts) == 2:
                seconds = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
    else:
        return None
    return seconds


def _normalise_units(seconds: float, clip_length: float) -> float:
    """按片段时长判定时间单位：取值远超片段长度时按毫秒折算。

    `clip_length` 必须来自真实片段（`end_seconds - start_seconds`），不能默认成 1：
    一个 300 秒的片段里出现 3200 秒，只可能是毫秒；而同样的 3200 在一个 3600 秒的
    片段里就是合法取值。
    """
    if clip_length <= 0:
        return seconds
    if abs(seconds) > clip_length * _UNIT_SUSPICION_RATIO + 1.0:
        return seconds / _MILLISECOND_FACTOR
    return seconds


def parse_segments(text: str) -> tuple[list[SpeechSegment], list[str]]:
    """解析模型输出为语音记录，并返回被丢弃条目的告警列表。

    这里只做**语法**层面的归一（键名、数值提取、字段完整性），不做任何时间单位判断或
    时间轴换算——那些判断需要片段长度，属于 `align_segments` 的职责。

    丢弃的条目一律留下告警文本，让「模型返回了 N 条、入库了 M 条」这件事可核查。
    """
    if not text.strip():
        raise ValueError("empty response")

    items = loads_segments(text)
    if items is None:
        raise ValueError("no json array in response")

    segments: list[SpeechSegment] = []
    warnings: list[str] = []

    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            warnings.append(f"第 {position} 条不是对象，已丢弃")
            continue

        raw_start = _to_seconds(_pick(item, _START_KEYS))
        raw_end = _to_seconds(_pick(item, _END_KEYS))
        content = _pick(item, _CONTENT_KEYS)
        # 语气是可选字段：模型没给、给成非字符串、给成空串都不算错，只是这条记录少一类线索
        raw_tone = _pick(item, _TONE_KEYS)
        tone = raw_tone.strip() if isinstance(raw_tone, str) else ""

        if raw_start is None or raw_end is None:
            warnings.append(f"第 {position} 条缺少可解析的开始或结束时间，已丢弃")
            continue
        if not isinstance(content, str) or not content.strip():
            warnings.append(f"第 {position} 条缺少语音内容，已丢弃")
            continue

        start, end = raw_start, raw_end
        if end - start < -_NEGATIVE_DURATION_TOLERANCE:
            warnings.append(
                f"第 {position} 条结束时间 {end:.1f}s 早于开始时间 {start:.1f}s，已丢弃"
            )
            continue
        if end < start:
            # 差值在容差内：模型把相邻句子的边界写反了，按瞬时语句收敛
            end = start

        segments.append(
            SpeechSegment(
                start_seconds=start, end_seconds=end, content=content.strip(), tone=tone
            )
        )

    segments.sort(key=lambda segment: segment.start_seconds)
    return segments, warnings


def parse_response(response: Any) -> UnderstandingResult:
    """把一次模型调用结果转成 `UnderstandingResult`。

    空输出、没有 JSON 数组都判为 `ValueError`——这些是失败，不能当成「片段里没人说话」
    落库，否则用户会把识别失败误读成静音片段。
    """
    text = extract_text(response)
    segments, warnings = parse_segments(text)

    usage: dict[str, Any] | None = None
    raw_usage = getattr(response, "usage", None)
    if raw_usage is not None:
        dumped = raw_usage.model_dump() if hasattr(raw_usage, "model_dump") else raw_usage
        if isinstance(dumped, dict):
            usage = dumped

    return UnderstandingResult(
        segments=segments,
        raw_text=text,
        usage=usage,
        warnings=warnings,
    )


def align_segments(
    result: UnderstandingResult, *, clip_start_seconds: float, clip_end_seconds: float
) -> list[AlignedSegment]:
    """把片段内的相对时间归一单位并平移为原视频的绝对时间。

    两件事都在这里做，因为它们共用同一个前提——片段长度：

    - **单位判定**：模型偶尔按毫秒给时间。`1500` 是 1500 秒还是 1.5 秒，只有对照片段
      时长才知道，因此不用固定阈值，而用「远超片段长度则按毫秒折算」。
    - **越界时段只标记不裁切**：裁切会让下游看到一段「看起来严丝合缝」的时间轴，而
      实际上模型并没有在那一秒说过那句话。
    """
    clip_length = max(clip_end_seconds - clip_start_seconds, 0.0)
    aligned: list[AlignedSegment] = []

    for segment in result.segments:
        start = _normalise_units(segment.start_seconds, clip_length)
        end = _normalise_units(segment.end_seconds, clip_length)
        if end < start:
            end = start

        out_of_range = (
            start < -_NEGATIVE_DURATION_TOLERANCE or end > clip_length + _NEGATIVE_DURATION_TOLERANCE
        )
        if out_of_range:
            logger.info(
                "片段时间越界：起点 %.1fs、终点 %.1fs 超出片段长度 %.1fs，仅标记不裁切",
                start,
                end,
                clip_length,
            )
        aligned.append(
            AlignedSegment(
                start_seconds=clip_start_seconds + start,
                end_seconds=clip_start_seconds + end,
                content=segment.content,
                tone=segment.tone,
                clip_start_seconds=start,
                clip_end_seconds=end,
                out_of_range=out_of_range,
            )
        )
    return aligned


# —— 复盘输出的解析（V0.3）——

# 分析等级：只认这三个取值，其它写法一律收敛，界面与文档才有确定的渲染口径
REVIEW_LEVELS = ("完整", "部分", "受限")
REVIEW_DEFAULT_LEVEL = "部分"

# 字段别名：模型可能用不同键名表达同一含义，按顺序取第一个可用的
_REVIEW_ALIASES: dict[str, tuple[str, ...]] = {
    "one_line": ("one_line", "summary", "one_line_summary", "conclusion", "verdict"),
    "analysis_level": ("analysis_level", "level"),
    "level_reason": ("level_reason", "analysis_level_reason", "level_basis", "basis"),
    "key_events": ("key_events", "events", "timeline", "key_moments"),
    "findings": ("findings", "observations", "dimensions", "analysis"),
    "top_issues": ("top_issues", "issues", "problems", "top3_issues"),
    "next_actions": ("next_actions", "actions", "experiments", "next_experiments"),
    "missing_info": ("missing_info", "gaps", "missing", "limitations"),
}

# 列表型字段的子项别名：子项字段缺失时按空字符串处理，不整条丢弃——列表本身有内容这件事
# 比某一句的措辞更重要，丢掉会让用户看不到模型确实发现了这个问题
_REVIEW_ITEM_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "key_events": {
        "topic": ("topic", "type", "category", "topic_type"),
        "summary": ("summary", "content", "description", "detail"),
        "start_seconds": ("start_seconds", "start_time", "start", "from"),
        "end_seconds": ("end_seconds", "end_time", "end", "to"),
    },
    "findings": {
        "dimension": ("dimension", "aspect", "category"),
        "judgement": ("judgement", "judgment", "finding", "conclusion", "observation"),
        "evidence_level": ("evidence_level", "level", "confidence", "evidence_type"),
        "evidence": ("evidence", "quote", "original", "proof"),
        "start_seconds": ("start_seconds", "start_time", "start", "time"),
        "suggestion": ("suggestion", "advice", "recommendation", "action"),
    },
    "top_issues": {
        "problem": ("problem", "issue", "fact", "description"),
        "evidence": ("evidence", "proof", "quote"),
        "impact": ("impact", "affected", "effect"),
        "root_cause": ("root_cause", "cause", "category"),
        "action": ("action", "fix", "improvement", "next"),
    },
    "next_actions": {
        "goal": ("goal", "objective", "target", "experiment_goal"),
        "how": ("how", "steps", "method", "when_and_what"),
        "observe": ("observe", "metrics", "indicators", "watch"),
    },
}

# 列表型字段的最大条数：准则要求「每场最多 TOP3」，超出的条目按模型给的顺序截断并记账
_REVIEW_ITEM_LIMITS = {"top_issues": 3, "next_actions": 3}


def _review_pick(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按候选键名从子项里取值；复用 `_pick` 的大小写不敏感与空值判定。"""
    return _pick(item, keys)


def _review_text(value: Any) -> str:
    """把取值转成展示文本：非字符串（数字、布尔）也转成文本，避免整条丢失。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _review_items(value: Any) -> list[dict[str, Any]]:
    """把列表型字段收敛为对象列表；非列表或含非对象项时丢弃非对象项。"""
    if isinstance(value, dict):
        # 只给了一个对象（如问题只有一条）：按单元素列表处理
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _review_seconds(value: Any) -> float | None:
    """时间取值转秒：复用语音记录的取值归一（含 `00:12.5` 这类写法）。"""
    return _to_seconds(value)


def normalise_review(payload: Any) -> tuple[dict[str, Any], list[str]]:
    """把模型返回的复盘对象归一为固定结构，并返回归一过程中的告警。

    归一的取向与语音记录一致：**能读出来的都留下，读不出来的如实记账**。缺少的列表字段
    补空数组、缺少的文本字段补空串，界面就不必到处判空；被截断或类型不符的条目留下告警，
    「模型给了 5 个问题、我们只展示 3 个」这件事必须可核查。
    """
    warnings: list[str] = []
    if not isinstance(payload, dict):
        raise ValueError("复盘输出不是 JSON 对象")

    lowered = {str(key).strip().lower(): value for key, value in payload.items()}

    def pick(field: str) -> Any:
        for alias in _REVIEW_ALIASES[field]:
            if alias in lowered and lowered[alias] not in (None, ""):
                return lowered[alias]
        return None

    result: dict[str, Any] = {
        "one_line": _review_text(pick("one_line")),
        "level_reason": _review_text(pick("level_reason")),
    }
    # 片段覆盖数由程序按真实结果写入（见 `tasks/review.py`），这里明确丢弃模型给的取值：
    # 模型看不到识别缺口，「它以为自己分析了多少片」不是可用的信息
    for field in ("clip_count", "succeeded_clip_count", "failed_clip_count"):
        if field in lowered:
            warnings.append(f"模型返回了 {field}，该字段以程序按真实片段结果计算为准，已忽略")

    # 分析等级：模型可能写「Level 2」「部分级」等变体，只认包含关键字的那三种
    raw_level = _review_text(pick("analysis_level"))
    level = next((candidate for candidate in REVIEW_LEVELS if candidate in raw_level), None)
    if level is None:
        level = REVIEW_DEFAULT_LEVEL
        warnings.append(f"分析等级取值无法识别（{raw_level or '缺失'}），按「{level}」处理")
    result["analysis_level"] = level

    for field in ("key_events", "findings", "top_issues", "next_actions", "missing_info"):
        if field == "missing_info":
            raw = pick(field)
            items = raw if isinstance(raw, list) else ([raw] if raw else [])
            result[field] = [_review_text(item) for item in items if _review_text(item)]
            continue

        items = _review_items(pick(field))
        limit = _REVIEW_ITEM_LIMITS.get(field)
        if limit is not None and len(items) > limit:
            warnings.append(f"{field} 返回 {len(items)} 条，按准则只保留前 {limit} 条")
            items = items[:limit]

        normalised: list[dict[str, Any]] = []
        for position, item in enumerate(items, start=1):
            aliases = _REVIEW_ITEM_ALIASES[field]
            entry = {name: _review_text(_review_pick(item, keys)) for name, keys in aliases.items()}
            for time_field in ("start_seconds", "end_seconds"):
                if time_field in entry:
                    entry[time_field] = _review_seconds(_review_pick(item, aliases[time_field]))
            if not any(entry.get(name) for name in ("summary", "judgement", "problem", "goal")):
                warnings.append(f"{field} 第 {position} 条缺少有效内容，已丢弃")
                continue
            normalised.append(entry)

        if field == "key_events":
            # 关键事件按时间升序：时间缺失的条目排在末尾而不是被丢掉
            normalised.sort(key=lambda entry: entry.get("start_seconds") if entry.get("start_seconds") is not None else float("inf"))
        result[field] = normalised

    return result, warnings


def parse_review_response(response: Any) -> ReviewResult:
    """把一次复盘调用结果转成 `ReviewResult`。

    非 JSON、解析不出对象一律判 `ValueError`（由客户端转成可重试的服务端错误）：把无法解析
    的返回落库成空结论，用户会看到一份「什么都没有」的复盘，与「复盘失败」无法区分。
    """
    text = extract_chat_text(response)
    if not text.strip():
        raise ValueError("empty response")

    payload = None
    for candidate in _iter_json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
        # 对象被包在数组里（模型偶尔如此）：取第一个对象
        if isinstance(parsed, list):
            first = next((item for item in parsed if isinstance(item, dict)), None)
            if first is not None:
                payload = first
                break

    if payload is None:
        raise ValueError("no json object in response")

    normalised, warnings = normalise_review(payload)

    usage: dict[str, Any] | None = None
    raw_usage = getattr(response, "usage", None)
    if raw_usage is not None:
        dumped = raw_usage.model_dump() if hasattr(raw_usage, "model_dump") else raw_usage
        if isinstance(dumped, dict):
            usage = dumped

    return ReviewResult(
        payload=normalised,
        raw_text=text,
        usage=usage,
        warnings=warnings,
    )
