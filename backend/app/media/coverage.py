"""整场覆盖校验：切分完成后检查片段是否真正覆盖了整场视频。

验收要求「片段交界、预期重叠与异常缺失可检查」，因此这里把可检查的问题逐条列出，而不是
只给一个布尔结论——问题列表能直接指向是第几片、缺了多少秒，便于定位与局部重切。

本模块只做整数/浮点比较，不依赖数据库与文件系统：片段记录由调用方传入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

# 边界容差：-c copy 的切点落在关键帧上，片段边界与计划值有百毫秒级偏差；1 秒容差既能
# 容忍这种偏差，又不会把真正的时间轴错误（漏掉整段内容）掩盖过去。
BOUNDARY_TOLERANCE_SECONDS = 1.0

ISSUE_MISSING_START = "missing_start"
ISSUE_MISSING_END = "missing_end"
ISSUE_GAP = "gap"
ISSUE_OVERLAP = "overlap"
ISSUE_ORDER = "order"


@dataclass(frozen=True)
class CoverageIssue:
    """一条覆盖问题：`code` 供程序判断，`message` 供展示与日志。"""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _issue(code: str, message: str) -> CoverageIssue:
    return CoverageIssue(code=code, message=message)


def check_coverage(
    total_duration_seconds: float | None,
    clips: Sequence[tuple[float, float]],
    *,
    tolerance_seconds: float = BOUNDARY_TOLERANCE_SECONDS,
) -> list[CoverageIssue]:
    """检查 `(起, 止)` 序列是否完整覆盖 `[0, total_duration_seconds]`。

    片段按传入顺序检查：顺序是片段序号的定义，乱序本身就是需要暴露的问题。
    """
    if not clips:
        return [_issue(ISSUE_MISSING_END, "没有任何片段，整场视频未被覆盖")]

    issues: list[CoverageIssue] = []

    first_start = clips[0][0]
    if first_start > tolerance_seconds:
        issues.append(
            _issue(
                ISSUE_MISSING_START,
                f"首个片段从 {first_start:.1f}s 开始，开头 {first_start:.1f}s 未被覆盖",
            )
        )

    for position, ((start, end), (next_start, _)) in enumerate(zip(clips, clips[1:])):
        if next_start < start:
            issues.append(
                _issue(
                    ISSUE_ORDER,
                    f"片段顺序异常：第 {position + 2} 片从 {next_start:.1f}s 开始，"
                    f"早于第 {position + 1} 片的 {start:.1f}s",
                )
            )
            continue

        if abs(next_start - end) <= tolerance_seconds:
            continue

        if next_start > end:
            issues.append(
                _issue(
                    ISSUE_GAP,
                    f"第 {position + 1} 片（止于 {end:.1f}s）与第 {position + 2} 片"
                    f"（起于 {next_start:.1f}s）之间缺失 {next_start - end:.1f}s",
                )
            )
        else:
            issues.append(
                _issue(
                    ISSUE_OVERLAP,
                    f"第 {position + 1} 片与第 {position + 2} 片重叠 {end - next_start:.1f}s，"
                    "同一内容会被重复处理",
                )
            )

    if total_duration_seconds is not None:
        last_end = clips[-1][1]
        if last_end < total_duration_seconds - tolerance_seconds:
            issues.append(
                _issue(
                    ISSUE_MISSING_END,
                    f"末个片段止于 {last_end:.1f}s，结尾 "
                    f"{total_duration_seconds - last_end:.1f}s 未被覆盖",
                )
            )

    return issues


def issues_to_json(issues: Sequence[CoverageIssue]) -> str | None:
    """问题列表入库形式；无问题时返回 None，使「无问题」与「未校验」可区分。"""
    if not issues:
        return None
    return json.dumps([issue.to_dict() for issue in issues], ensure_ascii=False)


def issues_from_json(raw: str | None) -> list[dict[str, str]]:
    """接口层把入库的问题列表还原为响应结构；内容异常时返回空列表而不是抛错。"""
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [
        {"code": str(item.get("code", "")), "message": str(item.get("message", ""))}
        for item in payload
        if isinstance(item, dict)
    ]


def summarize(issues: Sequence[CoverageIssue]) -> str:
    """一行摘要，用于日志与验收核对。"""
    if not issues:
        return "覆盖校验通过：片段完整覆盖整场视频"
    return f"覆盖校验发现 {len(issues)} 处问题：" + "；".join(issue.message for issue in issues)
