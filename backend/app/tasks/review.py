"""复盘链路（V0.3）：把整场语音转写交给文本模型，产出有依据的复盘结论。

设计要点：

- **输入只有语音**：`app/transcript.py` 把逐片记录拼成复盘输入文本；本模块不做任何内容
  加工，否则「模型看到的」与「用户核对时看到的」会不一致。
- **长直播分批**：单次请求装不下的转写按记录边界切批，逐批调用后合并。合并是**结构性**的
  （按维度分组、按时间排序、按准则截断 TOP3），不做二次摘要调用——多一次调用就多一处
  模型编造的机会，而合并规则本身是确定的。
- **失败片段不阻断**：缺口数量进模型输入、也写进结论的 `analysis_level` 与 `missing_info`。
  一片失败就让整场出不了复盘，用户拿不到任何结论，反而不如带着明确警示出结论。
- **复盘覆盖而不是追加**：换提示词或补完失败片段后重跑，结论以最后一次为准，与片段识别
  的重跑语义一致（历史版本留档只会让界面不知道该显示哪一份）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.llm import (
    REVIEW_SKIPPED,
    REVIEW_STATUS_FAILED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_RUNNING,
    REVIEW_STATUS_SUCCEEDED,
    LLMClientError,
    LLMError,
    ReviewClient,
    ReviewResult,
    build_review_system_prompt,
    build_review_user_prompt,
    describe_error,
    get_review_client,
)
from app.models import PROGRESS_REVIEW_START, Task, utcnow
from app.tasks.understanding import list_clips
from app.transcript import (
    batch_review_input,
    build_summary,
    segment_records,
)

logger = logging.getLogger(__name__)

# 复盘进度语义：认领 0 → 分批调用推进 10~90 → 落库完成 100。
# 起点 `PROGRESS_REVIEW_START` 定义在 `app/models.py`，理由同识别链。
PROGRESS_REVIEW_END = 90
PROGRESS_REVIEW_DONE = 100

# 分析等级：覆盖面不足时由程序下调，避免模型在缺片的情况下给出「完整」定级
LEVEL_FULL = "完整"
LEVEL_PARTIAL = "部分"
LEVEL_LIMITED = "受限"

# 覆盖率低于该比例时，无论模型怎么定级都下调为「受限」：说明转写本身不足以为结论背书
_LIMITED_COVERAGE_RATIO = 0.5

# 合并分批结果时的列表字段与「按时间排序」的字段
_LIST_FIELDS = ("key_events", "findings", "top_issues", "next_actions", "missing_info")
_TOP_LIMITS = {"top_issues": 3, "next_actions": 3}


def run_review(
    db: Session,
    task_id: str,
    *,
    client: ReviewClient | None = None,
    settings: Settings | None = None,
) -> None:
    """对任务执行一次复盘，返回时任务级复盘状态已落库。"""
    settings = settings or get_settings()
    task = db.get(Task, task_id)
    if task is None:
        logger.warning("复盘请求的任务不存在：%s", task_id)
        return

    clips = list_clips(db, task_id)
    summary = build_summary(task, clips, model_name=settings.llm_model_name)
    records = segment_records(summary.clips)

    if not records:
        _finish(
            db,
            task,
            status=REVIEW_SKIPPED,
            error="没有任何已识别的语音记录，无法复盘；请先完成识别",
        )
        return

    try:
        client = client or get_review_client()
    except LLMClientError as exc:
        _finish(db, task, status=REVIEW_STATUS_FAILED, error=describe_error(exc))
        logger.error("任务 %s 复盘无法开始：%s", task_id, describe_error(exc))
        return

    batches = batch_review_input(records, max_chars=settings.review_input_chars_per_call)
    # 状态取值与入队接口共用 `Task.mark_review_running()`；真实计数在本批之后写回，
    # 因为方法会把它们归零（入队时还不知道这批要投入多少条记录）。
    task.mark_review_running()
    task.review_segment_count = sum(1 for record in records if record["kind"] == "segment")
    task.review_clip_count = summary.succeeded_clip_count
    task.review_batch_count = len(batches)
    db.commit()

    system_prompt = build_review_system_prompt()
    results: list[ReviewResult] = []
    for position, text in enumerate(batches, start=1):
        user_prompt = build_review_user_prompt(
            filename=task.filename,
            duration_seconds=task.split_source_duration_seconds or task.duration_seconds,
            clip_count=summary.clip_count,
            succeeded_clip_count=summary.succeeded_clip_count,
            failed_clip_count=summary.failed_clip_count,
            segment_count=summary.segment_count,
            transcript=text,
            part=(position, len(batches)),
            total_parts=len(batches),
        )
        try:
            results.append(client.review(system_prompt=system_prompt, user_prompt=user_prompt))
        except Exception as exc:  # noqa: BLE001 —— 分批中任一批失败即整场失败
            _finish(
                db,
                task,
                status=REVIEW_STATUS_FAILED,
                error=_reason(exc),
                keep_counts=True,
            )
            logger.warning("任务 %s 复盘失败（第 %d/%d 批）：%s", task_id, position, len(batches), _reason(exc))
            return
        _advance(db, task, position, len(batches))

    payload = merge_reviews(results, summary=summary)
    # 覆盖面由程序按真实片段结果写入：模型看不到识别缺口，它若在输出里给了这几个字段
    # 一律以程序的计算为准——一份「模型以为自己分析了 10 片、实际只成功 3 片」的结论
    # 比没有结论更糟
    payload["clip_count"] = summary.clip_count
    payload["succeeded_clip_count"] = summary.succeeded_clip_count
    payload["failed_clip_count"] = summary.failed_clip_count
    _store(db, task, results, payload)

    logger.info(
        "任务 %s 复盘完成：%d 批，等级 %s，关键事件 %d 条，问题 %d 条",
        task_id,
        len(batches),
        payload.get("analysis_level"),
        len(payload.get("key_events") or []),
        len(payload.get("top_issues") or []),
    )


def merge_reviews(results: list[ReviewResult], *, summary) -> dict[str, Any]:
    """合并分批结论为一份结构化的整场复盘。

    合并是确定性的结构操作，不再调模型：

    - `one_line`：单批直接用；多批时拼出「前半场 / 后半场」的两句概括，并注明是分批分析。
    - 列表字段：按内容去重后拼接，`key_events` 按时间升序，`top_issues` / `next_actions`
      按准则截断到 3 条（**先到先得**，因为提示词已要求模型按重要性排序）。
    - `analysis_level`：取各批的**最保守**等级，再按真实覆盖率兜底下调——分批分析时
      任何一批「受限」都意味着整场结论站不住。
    """
    if len(results) == 1:
        payload = dict(results[0].payload)
    else:
        payload = {
            "one_line": _merge_one_line(results),
            "analysis_level": _most_conservative(results),
            "level_reason": "；".join(
                text for text in (_reason_text(result) for result in results) if text
            ),
        }
        for field in _LIST_FIELDS:
            collected: list[Any] = []
            for result in results:
                collected.extend(result.payload.get(field) or [])
            payload[field] = collected

    if results:
        deduped: list[ReviewResult] = []
        seen_reasons: set[str] = set()
        for result in results:
            reason = result.payload.get("level_reason", "").strip()
            if reason and reason in seen_reasons:
                continue
            seen_reasons.add(reason)
            deduped.append(result)
        results = deduped

    # 覆盖兜底：模型看不到缺口全貌（尤其是分批输入时），定级与依据由程序纠正，并**覆盖**
    # 模型给出的依据——否则会出现「等级已按全覆盖校正、依据却仍在说有缺口」这种自相矛盾。
    # 模型自己的判断作为第二句保留在后面，两条信息都要能看见。
    model_reason = _review_text(payload.get("level_reason"))
    level, note = _adjust_level(payload, summary)
    payload["analysis_level"] = level
    payload["level_reason"] = "；".join(filter(None, [note, model_reason]))

    # 缺口由程序写进「本场不足以判断」：这一段是面向用户的结论边界说明，不能只依赖
    # 模型自己意识到「它看到的转写是不完整的」——它看不到片段层面的失败记录
    gaps = payload.setdefault("missing_info", [])
    if summary.failed_clip_count:
        gaps.append(
            f"全场 {summary.succeeded_clip_count}/{summary.clip_count} 片识别成功，"
            f"{summary.failed_clip_count} 片的语音内容缺失，涉及这些时段的判断不完整"
        )
    if level == LEVEL_LIMITED:
        gaps.append(note)

    payload["key_events"] = _sort_events(_dedupe(payload.get("key_events") or []))
    payload["findings"] = _dedupe(payload.get("findings") or [])
    payload["missing_info"] = _dedupe(payload.get("missing_info") or [])
    for field, limit in _TOP_LIMITS.items():
        payload[field] = _dedupe(payload.get(field) or [])[:limit]

    payload["batch_count"] = len(results)
    return payload


def _merge_one_line(results: list[ReviewResult]) -> str:
    """多批时的一句话结论：按批序给出各批概括，并说明这是分段的结论。"""
    parts = [
        result.payload.get("one_line", "").strip()
        for result in results
        if result.payload.get("one_line", "").strip()
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    joined = "；".join(f"第 {index} 段：{text}" for index, text in enumerate(parts, start=1))
    return f"（本场转写分批分析）{joined}"


def _reason_text(result: ReviewResult) -> str:
    return result.payload.get("level_reason", "").strip()


def _review_text(value: Any) -> str:
    """取值转展示文本；非字符串一律按空处理（模型偶尔给对象）。"""
    return value.strip() if isinstance(value, str) else ""


def _most_conservative(results: list[ReviewResult]) -> str:
    order = {LEVEL_FULL: 0, LEVEL_PARTIAL: 1, LEVEL_LIMITED: 2}
    levels = [result.payload.get("analysis_level", LEVEL_PARTIAL) for result in results]
    return max(levels, key=lambda level: order.get(level, 1))


def _adjust_level(payload: dict[str, Any], summary) -> tuple[str, str]:
    """按真实覆盖面校正分析等级，返回（等级, 补充说明）。

    模型只看得到转写文本，看不到「有多少片段没识别成功」，因此定级与定级依据都不能由它
    单方面说了算：**依据一律由程序按真实覆盖面重写**，并把理由写进结论，让用户知道结论的
    边界在哪。缺片时降级；全覆盖时也明确写出覆盖情况，避免模型沿用自己的措辞把「全部
    识别成功」说成有缺口。
    """
    total = summary.clip_count
    succeeded = summary.succeeded_clip_count
    failed = summary.failed_clip_count
    requested = payload.get("analysis_level", LEVEL_PARTIAL)

    if total > 0 and succeeded / total < _LIMITED_COVERAGE_RATIO:
        return LEVEL_LIMITED, (
            f"仅 {succeeded}/{total} 片识别成功，转写覆盖不足，"
            "本场只给主题与结构层面的观察，不评价具体话术与表达细节"
        )

    if failed:
        note = f"{failed}/{total} 片识别失败，相关时段内容缺失，涉及这些时段的结论仅为推断"
        return (LEVEL_PARTIAL if requested == LEVEL_FULL else requested), note

    confirmation = f"全场 {total} 片识别成功，转写覆盖完整"
    if requested == LEVEL_LIMITED:
        # 覆盖面没问题却自评受限：尊重模型对文本本身质量的判断，但把覆盖情况写清楚
        return LEVEL_LIMITED, f"{confirmation}；模型判定转写内容本身不足以支撑更细的分析"
    return requested, confirmation


def _dedupe(items: list[Any]) -> list[Any]:
    """按内容去重并保持顺序：分批结果的边界处常有重复项。"""
    seen: set[str] = set()
    unique: list[Any] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _sort_events(events: list[Any]) -> list[Any]:
    """关键事件按时间升序；没有时间的排在末尾而不是丢掉。"""

    def key(event: Any) -> float:
        if isinstance(event, dict):
            value = event.get("start_seconds")
            if isinstance(value, (int, float)):
                return float(value)
        return float("inf")

    return sorted(events, key=key)


def _store(db: Session, task: Task, results: list[ReviewResult], payload: dict[str, Any]) -> None:
    """落库：结构化结论 + 各批原始返回 + 用量 + 归一告警。"""
    usage = [result.usage for result in results if result.usage]
    warnings = [warning for result in results for warning in result.warnings]
    if warnings:
        payload["warnings"] = warnings

    task.review_result_json = json.dumps(payload, ensure_ascii=False)
    task.review_raw_text = "\n\n".join(
        f"—— 第 {index} 批 ——\n{result.raw_text}" for index, result in enumerate(results, start=1)
    )
    task.review_usage_json = json.dumps(usage, ensure_ascii=False) if usage else None
    task.review_status = REVIEW_STATUS_SUCCEEDED
    task.review_error = None
    task.review_progress = PROGRESS_REVIEW_DONE
    task.review_finished_at = utcnow()
    task.updated_at = utcnow()
    db.commit()


def _advance(db: Session, task: Task, done: int, total: int) -> None:
    if total <= 0:
        return
    span = PROGRESS_REVIEW_END - PROGRESS_REVIEW_START
    task.review_progress = PROGRESS_REVIEW_START + int(span * done / total)
    task.updated_at = utcnow()
    db.commit()


def _reason(exc: BaseException) -> str:
    if isinstance(exc, LLMError):
        return f"{type(exc).__name__}: {describe_error(exc)}"
    return f"{type(exc).__name__}: {exc}"


def _finish(
    db: Session,
    task: Task,
    *,
    status: str,
    error: str | None = None,
    keep_counts: bool = False,
) -> None:
    """任务级终态落库；`keep_counts` 为真时保留本次已送出的覆盖面统计。"""
    task.review_status = status
    task.review_error = error
    task.review_progress = PROGRESS_REVIEW_DONE if status == REVIEW_STATUS_FAILED else 0
    task.review_finished_at = utcnow()
    if not keep_counts:
        task.review_segment_count = 0
        task.review_clip_count = 0
        task.review_batch_count = 0
    task.updated_at = utcnow()
    db.commit()


def reset_task_review(db: Session, task_id: str) -> Task | None:
    """重跑前清空上一轮结论：避免「上一次成功、这一次失败」却仍展示旧结论。"""
    task = db.get(Task, task_id)
    if task is None:
        return None
    task.review_status = REVIEW_STATUS_PENDING
    task.review_error = None
    task.review_progress = 0
    task.review_segment_count = 0
    task.review_clip_count = 0
    task.review_batch_count = 0
    task.review_started_at = None
    task.review_finished_at = None
    task.updated_at = utcnow()
    db.commit()
    return task


def load_review_result(task: Task) -> dict[str, Any] | None:
    """读取任务的复盘结论；未复盘或内容损坏时返回 None。"""
    if not task.review_result_json:
        return None
    try:
        payload = json.loads(task.review_result_json)
    except ValueError:
        logger.warning("任务 %s 的复盘结论无法解析，按无结论处理", task.id)
        return None
    return payload if isinstance(payload, dict) else None


def review_to_markdown(task: Task, payload: dict[str, Any], *, model_name: str | None = None) -> str:
    """把复盘结论渲染为 Markdown，供下载与归档。

    与界面同源：渲染用的就是落库的那份结构化结论，因此「页面看到的」与「下载到的」一致。
    """
    from app.transcript import format_timestamp

    lines: list[str] = [f"# 直播复盘：{task.filename or task.id}", ""]
    lines.append(f"- 分析等级：{payload.get('analysis_level', '未定级')}")
    if payload.get("level_reason"):
        lines.append(f"- 定级依据：{payload['level_reason']}")
    lines.append(
        f"- 覆盖面：片段 {payload.get('succeeded_clip_count', 0)}"
        f"/{payload.get('clip_count', 0)} 片识别成功，"
        f"投入分析 {task.review_segment_count} 条语音记录"
    )
    if payload.get("batch_count", 1) > 1:
        lines.append(f"- 分批分析：{payload['batch_count']} 批")
    if model_name:
        lines.append(f"- 模型：{model_name}")
    lines.append("")

    if payload.get("one_line"):
        lines.extend(["## 本场一句话结论", "", payload["one_line"], ""])

    events = payload.get("key_events") or []
    if events:
        lines.extend(["## 关键事件", ""])
        for event in events:
            start = event.get("start_seconds")
            stamp = format_timestamp(float(start)) if isinstance(start, (int, float)) else "时间未给出"
            lines.append(f"- [{stamp}] {event.get('topic', '')}｜{event.get('summary', '')}")
        lines.append("")

    findings = payload.get("findings") or []
    if findings:
        lines.extend(["## 分析发现", ""])
        for finding in findings:
            start = finding.get("start_seconds")
            stamp = format_timestamp(float(start)) if isinstance(start, (int, float)) else "时间未给出"
            lines.append(
                f"- **{finding.get('dimension', '')}**｜{finding.get('judgement', '')}"
                f"（{finding.get('evidence_level', '')}，{stamp}）"
            )
            if finding.get("evidence"):
                lines.append(f"  - 依据：{finding['evidence']}")
            if finding.get("suggestion"):
                lines.append(f"  - 建议：{finding['suggestion']}")
        lines.append("")

    issues = payload.get("top_issues") or []
    if issues:
        lines.extend(["## TOP 问题", ""])
        for position, issue in enumerate(issues, start=1):
            lines.append(f"{position}. {issue.get('problem', '')}")
            for label, key in (
                ("证据", "evidence"),
                ("影响", "impact"),
                ("根因", "root_cause"),
                ("下一场动作", "action"),
            ):
                if issue.get(key):
                    lines.append(f"   - {label}：{issue[key]}")
        lines.append("")

    actions = payload.get("next_actions") or []
    if actions:
        lines.extend(["## 下一场实验动作", ""])
        for position, action in enumerate(actions, start=1):
            lines.append(f"{position}. 目标：{action.get('goal', '')}")
            if action.get("how"):
                lines.append(f"   - 怎么做：{action['how']}")
            if action.get("observe"):
                lines.append(f"   - 观察：{action['observe']}")
        lines.append("")

    missing = payload.get("missing_info") or []
    if missing:
        lines.extend(["## 本场不足以判断", ""])
        lines.extend(f"- {item}" for item in missing)
        lines.append("")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.extend(["## 解析告警", ""])
        lines.extend(f"- {item}" for item in warnings)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "PROGRESS_REVIEW_DONE",
    "PROGRESS_REVIEW_END",
    "PROGRESS_REVIEW_START",
    "load_review_result",
    "merge_reviews",
    "reset_task_review",
    "review_to_markdown",
    "run_review",
]
