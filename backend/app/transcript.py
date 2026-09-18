"""识别结果汇总：把逐片的语音记录拼成整场全文。

全文是「识别结果汇总」的对外产物，只在这里生成一处，供接口、导出与页面共用同一份内容，
不至于出现页面看到 A、复制得到 B 的情况。

拼装规则刻意为可核查服务：

- **按原视频时间顺序**：片段按序号、片段内条目按开始时间排序，拼接结果就是一条时间线。
- **失败片段显式留位**：缺的那一段写成一行括注，而不是静默跳过。「整场识别」的完整度
  必须能被一眼看出，否则用户会把缺失当成「这段时间没人说话」。
- **时间只做展示格式化**，不做任何插值或对齐猜测——它只是证据的坐标。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.models import (
    UNDERSTANDING_STATUS_FAILED,
    UNDERSTANDING_STATUS_SUCCEEDED,
    MediaClip,
    Task,
)

logger = logging.getLogger(__name__)


@dataclass
class ClipBlock:
    """一个片段在汇总里的呈现：含失败片段。"""

    index: int
    start_seconds: float
    end_seconds: float
    status: str
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def segment_count(self) -> int:
        return len(self.segments)


@dataclass
class TranscriptSummary:
    """整场汇总：片段状态 + 展开后的条目 + 全文文本。"""

    task_id: str
    filename: str | None
    status: str
    model_name: str | None
    clips: list[ClipBlock]
    segments: list[dict[str, Any]]
    text: str

    @property
    def clip_count(self) -> int:
        return len(self.clips)

    @property
    def succeeded_clip_count(self) -> int:
        return sum(1 for clip in self.clips if clip.status == UNDERSTANDING_STATUS_SUCCEEDED)

    @property
    def failed_clip_count(self) -> int:
        return sum(1 for clip in self.clips if clip.status == UNDERSTANDING_STATUS_FAILED)

    @property
    def segment_count(self) -> int:
        return len(self.segments)


def format_timestamp(seconds: float) -> str:
    """秒数转 `时:分:秒.毫秒`；负数（模型给出的越界起点）按 0 显示并保留符号外的信息。

    时长在一小时内的直播占多数，但两三个小时的场次同样在范围内，因此一律带小时位，
    避免同一份全文里出现两种时间格式。
    """
    negative = seconds < 0
    value = abs(seconds)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    whole = int(value % 60)
    millis = int(round((value - int(value)) * 1000))
    # 四舍五入到 1000ms 时进位到秒
    if millis == 1000:
        millis = 0
        whole += 1
    stamp = f"{hours:02d}:{minutes:02d}:{whole:02d}.{millis:03d}"
    return f"-{stamp}" if negative else stamp


def load_segments(clip: MediaClip) -> list[dict[str, Any]]:
    """读取片段的语音记录；内容损坏时按空处理并记日志，不让汇总整体失败。"""
    if not clip.understanding_segments_json:
        return []
    try:
        payload = json.loads(clip.understanding_segments_json)
    except ValueError:
        logger.warning("片段 %d 的识别记录无法解析，按空处理", clip.index)
        return []
    if not isinstance(payload, list):
        logger.warning("片段 %d 的识别记录不是数组，按空处理", clip.index)
        return []
    return [item for item in payload if isinstance(item, dict)]


def load_warnings(clip: MediaClip) -> list[str]:
    """读取片段的解析告警；损坏时按空处理。"""
    if not clip.understanding_warnings_json:
        return []
    try:
        payload = json.loads(clip.understanding_warnings_json)
    except ValueError:
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []


def build_summary(task: Task, clips: list[MediaClip], *, model_name: str | None = None) -> TranscriptSummary:
    """把任务的片段记录汇总成整场结果。"""
    blocks: list[ClipBlock] = []
    flat: list[dict[str, Any]] = []

    for clip in clips:
        block = ClipBlock(
            index=clip.index,
            start_seconds=clip.start_seconds,
            end_seconds=clip.end_seconds,
            status=clip.understanding_status,
            error=clip.understanding_error,
            warnings=load_warnings(clip),
            segments=load_segments(clip) if clip.understanding_succeeded else [],
        )
        block.segments.sort(key=lambda item: float(item.get("start_seconds") or 0.0))
        blocks.append(block)

        for segment in block.segments:
            flat.append({**segment, "clip_index": clip.index})

    return TranscriptSummary(
        task_id=task.id,
        filename=task.filename,
        status=task.understanding_status,
        model_name=model_name,
        clips=blocks,
        segments=flat,
        text=render_text(task, blocks, model_name=model_name),
    )


def render_text(task: Task, blocks: list[ClipBlock], *, model_name: str | None = None) -> str:
    """把片段记录渲染为可复制的纯文本全文。"""
    clips = sorted(blocks, key=lambda block: block.index)
    succeeded = sum(1 for block in clips if block.status == UNDERSTANDING_STATUS_SUCCEEDED)
    failed = sum(1 for block in clips if block.status == UNDERSTANDING_STATUS_FAILED)
    total_segments = sum(block.segment_count for block in clips)

    lines: list[str] = [
        f"# 语音识别全文：{task.filename or task.id}",
        f"# 片段数 {len(clips)}，已识别 {succeeded}，失败 {failed}，语音记录 {total_segments} 条",
    ]
    if model_name:
        lines.append(f"# 模型：{model_name}")
    lines.append("")

    if not clips:
        lines.append("（暂无识别结果）")
        return "\n".join(lines).rstrip() + "\n"

    if succeeded == 0 and failed == 0:
        # 有切片但一片都没识别过：这是「还没开始」，不是「识别出来是空的」
        lines.append("（整场尚未识别，请先启动识别）")
        return "\n".join(lines).rstrip() + "\n"

    for block in clips:
        span = f"{format_timestamp(block.start_seconds)} - {format_timestamp(block.end_seconds)}"
        if block.status == UNDERSTANDING_STATUS_FAILED:
            reason = block.error or "原因未记录"
            lines.append(f"── 片段 {block.index}（{span}）识别失败：{reason}")
            lines.append("")
            continue
        if block.status != UNDERSTANDING_STATUS_SUCCEEDED:
            lines.append(f"── 片段 {block.index}（{span}）尚未识别")
            lines.append("")
            continue

        lines.append(f"── 片段 {block.index}（{span}），{block.segment_count} 条语音")
        if block.segment_count == 0:
            lines.append("（该片段未识别出人声语音）")
        for segment in block.segments:
            start = format_timestamp(float(segment.get("start_seconds") or 0.0))
            end = format_timestamp(float(segment.get("end_seconds") or 0.0))
            content = str(segment.get("content") or "").strip()
            tone = str(segment.get("tone") or "").strip()
            marker = "  ⚠ 时间越界" if segment.get("out_of_range") else ""
            tone_note = f"（语气：{tone}）" if tone else ""
            lines.append(f"[{start} - {end}] {content}{tone_note}{marker}")
        for warning in block.warnings:
            lines.append(f"（解析告警：{warning}）")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# —— 复盘输入（V0.3）——

# 每条语音记录在复盘输入里的行首标记：时间 + 语气。语气缺失时只留时间
_REVIEW_LINE = "[{start}] {content}{tone}"


def render_review_text(summary: TranscriptSummary) -> str:
    """把整场记录渲染为**复盘节点**的输入文本。

    与 `render_text` 分开而不是复用：那份全文是给人看的（分片段落、告警括注、越界标记，
    便于逐条核对），这里给模型看，取向不同：

    - **紧凑**：一行一条记录，不带片段分隔与解析告警——这些对分析是噪声，token 却要照付。
    - **保留语气**：V0.3 起识别会给出每句的语气估计，它是准则里「语气与情绪」「互动留人」
      两个维度唯一的输入。
    - **覆盖缺口仍要说明**：失败与未识别的片段用一行括注明确写出，否则模型会把「缺失」
      当成「这段时间没人说话」，进而给出「内容空洞」这类错误结论。
    """
    clips = sorted(summary.clips, key=lambda block: block.index)
    lines: list[str] = []
    succeeded = sum(1 for block in clips if block.status == UNDERSTANDING_STATUS_SUCCEEDED)
    failed = sum(1 for block in clips if block.status == UNDERSTANDING_STATUS_FAILED)
    pending = len(clips) - succeeded - failed

    lines.append(
        f"全场共 {len(clips)} 片，识别成功 {succeeded} 片，失败 {failed} 片，未识别 {pending} 片。"
    )
    if failed:
        spans = "、".join(
            f"片段 {block.index}（{format_timestamp(block.start_seconds)} - "
            f"{format_timestamp(block.end_seconds)}）"
            for block in clips
            if block.status == UNDERSTANDING_STATUS_FAILED
        )
        lines.append(f"以下时段语音识别失败，内容缺失，不要据此外推或补全：{spans}。")
    lines.append("")

    for block in clips:
        if block.status != UNDERSTANDING_STATUS_SUCCEEDED:
            continue
        for segment in block.segments:
            start = format_timestamp(float(segment.get("start_seconds") or 0.0))
            content = str(segment.get("content") or "").strip()
            if not content:
                continue
            tone = str(segment.get("tone") or "").strip()
            suffix = f"（语气：{tone}）" if tone else ""
            lines.append(_REVIEW_LINE.format(start=start, content=content, tone=suffix))

    return "\n".join(lines).rstrip() + "\n"


def segment_records(clips: list[ClipBlock]) -> list[dict[str, Any]]:
    """按时间顺序展开已识别片段里的语音记录（复盘输入分批用）。

    返回的列表里只有**真正有内容的记录**：片段分隔行只在该片确有语音记录时才带上。
    否则一场「所有片段都识别成空」的直播会带着一堆只有分隔行的输入进入复盘——那等于
    拿空输入问模型，而正确的结果是「没有可分析的内容」。
    """
    records: list[dict[str, Any]] = []
    for block in sorted(clips, key=lambda item: item.index):
        if block.status != UNDERSTANDING_STATUS_SUCCEEDED:
            continue
        lines: list[str] = []
        for segment in block.segments:
            content = str(segment.get("content") or "").strip()
            if not content:
                continue
            tone = str(segment.get("tone") or "").strip()
            suffix = f"（语气：{tone}）" if tone else ""
            lines.append(
                _REVIEW_LINE.format(
                    start=format_timestamp(float(segment.get("start_seconds") or 0.0)),
                    content=content,
                    tone=suffix,
                )
            )
        if lines:
            records.append(
                {"kind": "clip", "clip_index": block.index, "text": _clip_header(block)}
            )
            records.extend(
                {"kind": "segment", "clip_index": block.index, "text": line} for line in lines
            )
    return records


def _clip_header(block: ClipBlock) -> str:
    """片段在复盘输入里的分隔行：标记「这是第几片、覆盖哪段」，便于结论引用时间。"""
    return (
        f"── 片段 {block.index}"
        f"（{format_timestamp(block.start_seconds)} - {format_timestamp(block.end_seconds)}）"
    )


def batch_review_input(records: list[dict[str, Any]], *, max_chars: int) -> list[str]:
    """把展开后的记录按字符上限切分成若干批，返回每批的文本。

    逐行累加，加不下就开新批：行本身是**不可再分的最小单位**（一条语音记录或一个片段头），
    一条超长句只会独占一批，不会被劈成两半分别送给模型。上限只作分批阈值，不做硬截断。
    """
    if max_chars <= 0:
        return ["\n".join(record["text"] for record in records)] if records else []

    batches: list[str] = []
    current: list[str] = []
    size = 0

    for record in records:
        text = record["text"]
        if current and size + len(text) > max_chars:
            batches.append("\n".join(current))
            current = []
            size = 0
        current.append(text)
        size += len(text) + 1

    if current:
        batches.append("\n".join(current))
    return batches
