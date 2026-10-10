"""复盘链路（V0.3 起；V0.8.0 改为分轮专项调用）。

设计要点：

- **输入只有语音**：`app/transcript.py` 把逐片记录拼成复盘输入文本；本模块不做任何内容
  加工，否则「模型看到的」与「用户核对时看到的」会不一致。
- **分四轮专项调用**（V0.8.0）：结构 → 六维检核 → 归因实验 → 金句采集。每轮只回答一件事，
  理由见 `review_prompts.py` 顶部——把全部检核项塞进一次推理会让每块都写不深。归因轮的
  输入是前两轮的**结构化产出**而不是转写全文，因此它的产出规模与直播时长无关。
- **长直播分批**：吃转写的三轮（结构 / 检核 / 金句）按记录边界切批，逐批调用后做**结构性**
  合并（按维度分组、按时间排序、按准则截断 TOP3），不做二次摘要调用——多一次调用就多一处
  模型编造的机会，而合并规则本身是确定的。
- **分轮产物独立落库**（`Task.review_passes_json`）：某一轮失败时，已完成轮次的产物必须留下，
  重试只重跑缺的那一轮。这是「失败隔离」在分轮架构下的具体形式，也是允许多轮调用之后仍然
  划算的前提。
- **失败片段不阻断**：缺口数量进模型输入、也写进结论的 `analysis_level` 与 `missing_info`。
- **整轮重跑才覆盖**：用户点「重新复盘」时清空分轮产物，结论以最后一次为准；自动补跑与重启
  续跑则保留已完成的轮次。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.llm import (
    PASS_ATTRIBUTION,
    PASS_CHECKS,
    PASS_QUOTES,
    PASS_STRUCTURE,
    REVIEW_PASSES,
    REVIEW_SKIPPED,
    REVIEW_STATUS_FAILED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_RUNNING,
    REVIEW_STATUS_SUCCEEDED,
    TRANSCRIPT_PASSES,
    LLMClientError,
    LLMError,
    ReviewClient,
    ReviewResult,
    build_pass_system_prompt,
    build_pass_user_prompt,
    describe_error,
    get_review_client,
)
from app.llm.mindset import MIND_SETS, sample_by_code
from app.models import PROGRESS_REVIEW_START, Task, utcnow
from app.tasks.understanding import list_clips
from app.transcript import (
    batch_review_input,
    build_summary,
    segment_records,
)

logger = logging.getLogger(__name__)

# 复盘进度语义：认领 0 → 逐轮推进 10~90 → 落库完成 100。
PROGRESS_REVIEW_END = 90
PROGRESS_REVIEW_DONE = 100

# 分析等级：覆盖面不足时由程序下调，避免模型在缺片的情况下给出「完整」定级
LEVEL_FULL = "完整"
LEVEL_PARTIAL = "部分"
LEVEL_LIMITED = "受限"

# 覆盖率低于该比例时，无论模型怎么定级都下调为「受限」
_LIMITED_COVERAGE_RATIO = 0.5

_TOP_LIMITS = {"top_issues": 3, "next_actions": 3}

# 分轮产物的落库形状：`{"calls": 调用次数, "passes": {轮次名: 该轮结论}}`。
# 用外层对象而不是直接把轮次名摊平，是为了让「调用次数」这类计数有地方放，而不必混进轮次名
# 空间里（混进去就会出现「有个叫 calls 的轮次」这种歧义）。
_STATE_CALLS = "calls"
_STATE_PASSES = "passes"


def run_review(
    db: Session,
    task_id: str,
    *,
    client: ReviewClient | None = None,
    settings: Settings | None = None,
) -> None:
    """对任务执行一次分轮复盘，返回时任务级复盘状态已落库。"""
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

    task.mark_review_running()
    task.review_segment_count = sum(1 for record in records if record["kind"] == "segment")
    task.review_clip_count = summary.succeeded_clip_count
    stored, calls = _load_state(task)
    task.review_batch_count = calls
    db.commit()

    total_passes = len(REVIEW_PASSES)
    for position, pass_name in enumerate(REVIEW_PASSES, start=1):
        if pass_name in stored:
            # 续跑：这一轮已经有过结论（上一轮跑到这里失败、或进程被打断）。跳过它，
            # 不重复花这一次推理的钱。
            logger.info("任务 %s 的复盘跳过已完成的 %s 轮", task_id, pass_name)
            _advance(db, task, position, total_passes)
            continue

        try:
            payload, used = _run_pass(
                pass_name,
                task=task,
                summary=summary,
                batches=batches,
                client=client,
                stored=stored,
            )
        except Exception as exc:  # noqa: BLE001 —— 任一轮失败即整场失败，已完成轮次保留
            _finish(db, task, status=REVIEW_STATUS_FAILED, error=_reason(exc), keep_counts=True)
            logger.warning("任务 %s 复盘失败（%s 轮）：%s", task_id, pass_name, _reason(exc))
            return

        stored[pass_name] = payload
        calls += used
        _save_state(db, task, stored, calls)
        _advance(db, task, position, total_passes)

    payload = merge_passes(stored, summary=summary)
    payload["batch_count"] = calls
    _store(db, task, stored, calls, payload)

    logger.info(
        "任务 %s 复盘完成：%d 次调用，等级 %s，关键事件 %d 条，发现 %d 条，问题 %d 条，金句 %d 条",
        task_id,
        calls,
        payload.get("analysis_level"),
        len(payload.get("key_events") or []),
        len(payload.get("findings") or []),
        len(payload.get("top_issues") or []),
        len(payload.get("quotes") or []),
    )


def _run_pass(
    pass_name: str,
    *,
    task: Task,
    summary,
    batches: list[str],
    client: ReviewClient,
    stored: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """跑一轮，返回（该轮归一后的结论, 本轮实际调用次数）。

    吃转写的轮次逐批调用后结构性合并；归因轮是单次调用，输入是前两轮的结构化产出。
    """
    system_prompt = build_pass_system_prompt(pass_name)

    if pass_name in TRANSCRIPT_PASSES:
        collected: list[dict[str, Any]] = []
        for position, text in enumerate(batches, start=1):
            user_prompt = build_pass_user_prompt(
                pass_name,
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
            result = client.review(system_prompt=system_prompt, user_prompt=user_prompt)
            collected.append(_payload_of(result))
        return _merge_pass(pass_name, collected), len(batches)

    # 归因轮：单次调用，输入是前两轮的产出
    user_prompt = build_pass_user_prompt(
        pass_name,
        filename=task.filename,
        duration_seconds=task.split_source_duration_seconds or task.duration_seconds,
        clip_count=summary.clip_count,
        succeeded_clip_count=summary.succeeded_clip_count,
        failed_clip_count=summary.failed_clip_count,
        segment_count=summary.segment_count,
        prior=_attribution_prior(stored),
    )
    result = client.review(system_prompt=system_prompt, user_prompt=user_prompt)
    return _merge_pass(pass_name, [_payload_of(result)]), 1


def _payload_of(result: ReviewResult) -> dict[str, Any]:
    """取一次调用的归一结论；非字典一律按空结论处理（解析器已保证是字典）。"""
    payload = result.payload
    return payload if isinstance(payload, dict) else {}


def _merge_pass(pass_name: str, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一轮的多个批次结论合并成该轮的结论。

    合并规则是确定性的，不调模型：列表字段去重拼接、按需排序与截断，等级取最保守的一档。
    """
    if pass_name == PASS_STRUCTURE:
        return {
            "one_line": _merge_one_line(payloads),
            "analysis_level": _most_conservative(payloads),
            "level_reason": "；".join(
                text for text in (_text(payload.get("level_reason")) for payload in payloads) if text
            ),
            "key_events": _sort_events(_dedupe(_collect(payloads, "key_events"))),
            "topic_distribution": "；".join(
                text
                for text in (_text(payload.get("topic_distribution")) for payload in payloads)
                if text
            ),
            # 缺口清单由结构轮产出、程序再补识别缺口：这份清单是面向用户的结论边界说明，
            # 不能只在某一轮里写而没人带下来
            "missing_info": _dedupe(_collect(payloads, "missing_info")),
        }
    if pass_name == PASS_CHECKS:
        return {
            "findings": _dedupe(_collect(payloads, "findings")),
            "repetition": _merge_repetition(_collect(payloads, "repetition")),
            "violations": _dedupe(_collect(payloads, "violations")),
        }
    if pass_name == PASS_ATTRIBUTION:
        return {
            "top_issues": _dedupe(_collect(payloads, "top_issues"))[:_TOP_LIMITS["top_issues"]],
            "next_actions": _dedupe(_collect(payloads, "next_actions"))[:_TOP_LIMITS["next_actions"]],
        }
    if pass_name == PASS_QUOTES:
        return {
            "quotes": _dedupe(_collect(payloads, "quotes")),
        }
    raise KeyError(f"未知的复盘轮次：{pass_name}")


def merge_passes(stored: dict[str, Any], *, summary) -> dict[str, Any]:
    """把各轮结论拼成一份整场复盘，并做程序侧兜底（覆盖率定级、缺口登记）。

    拼接本身只是取各轮的字段——真正的判断在覆盖兜底那一段：模型看不到识别缺口，定级与
    缺口说明不能由它单方面说了算。
    """
    payload: dict[str, Any] = {}
    for pass_name in REVIEW_PASSES:
        pass_payload = stored.get(pass_name)
        if isinstance(pass_payload, dict):
            payload.update(pass_payload)

    payload.setdefault("one_line", "")
    payload.setdefault("level_reason", "")
    for field in ("key_events", "findings", "top_issues", "next_actions", "quotes", "violations", "repetition"):
        payload.setdefault(field, [])
    payload.setdefault("topic_distribution", "")

    # 覆盖面由程序按真实片段结果写入：模型看不到识别缺口
    payload["clip_count"] = summary.clip_count
    payload["succeeded_clip_count"] = summary.succeeded_clip_count
    payload["failed_clip_count"] = summary.failed_clip_count

    model_reason = _text(payload.get("level_reason"))
    level, note = _adjust_level(payload, summary)
    payload["analysis_level"] = level
    payload["level_reason"] = "；".join(filter(None, [note, model_reason]))

    # 缺口由程序写进「本场不足以判断」：这一段是面向用户的结论边界说明，不能只依赖模型
    # 自己意识到「它看到的转写是不完整的」——它看不到片段层面的失败记录
    gaps: list[str] = list(payload.get("missing_info") or [])
    if summary.failed_clip_count:
        gaps.append(
            f"全场 {summary.succeeded_clip_count}/{summary.clip_count} 片识别成功，"
            f"{summary.failed_clip_count} 片的语音内容缺失，涉及这些时段的判断不完整"
        )
    if level == LEVEL_LIMITED:
        gaps.append(note)
    payload["missing_info"] = _dedupe(gaps)

    # 分轮信息留档：界面上「本场跑了哪几轮」是可解释项，出问题时第一个要问的就是它
    payload["passes_run"] = [
        name for name in REVIEW_PASSES if isinstance(stored.get(name), dict)
    ]
    return payload


def _attribution_prior(stored: dict[str, Any]) -> str:
    """归因轮的输入：结构轮与检核轮的产出，去掉与本轮无关的字段。

    只给需要的字段，是因为这一段会进模型上下文，把覆盖数、批次这类程序侧信息一并送进去
    只会挤占注意力。
    """
    keep = ("one_line", "key_events", "topic_distribution", "findings", "repetition", "violations")
    merged: dict[str, Any] = {}
    for name in (PASS_STRUCTURE, PASS_CHECKS):
        payload = stored.get(name)
        if not isinstance(payload, dict):
            continue
        for key in keep:
            value = payload.get(key)
            if value:
                merged[key] = value
    return json.dumps(merged, ensure_ascii=False, indent=1)


def _collect(payloads: list[dict[str, Any]], field: str) -> list[Any]:
    """把各批次的某个列表字段拼起来。"""
    collected: list[Any] = []
    for payload in payloads:
        value = payload.get(field)
        if isinstance(value, list):
            collected.extend(value)
    return collected


def _merge_repetition(items: list[Any]) -> list[Any]:
    """合并跨批次的重复度记录：同一条卖点跨批被分别统计时，次数相加、增量取或。

    直接拼接会得到两行同名卖点、次数各算一半，读起来像「各重复了两次」——而事实是它在整场
    被讲了更多次。次数是最需要可信的一项，因此这里做真正的合并而不是去重。
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _text(item.get("item")) or json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
            continue
        current = merged[key]
        current["count"] = _as_int(current.get("count")) + _as_int(item.get("count"))
        current["has_increment"] = bool(current.get("has_increment")) or bool(item.get("has_increment"))
        extra = _text(item.get("times"))
        if extra:
            existing = _text(current.get("times"))
            current["times"] = f"{existing} / {extra}" if existing else extra
        extra_verdict = _text(item.get("verdict"))
        if extra_verdict and extra_verdict not in _text(current.get("verdict")):
            existing = _text(current.get("verdict"))
            current["verdict"] = f"{existing}；{extra_verdict}" if existing else extra_verdict
    return [merged[key] for key in order]


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _merge_one_line(payloads: list[dict[str, Any]]) -> str:
    """多批时的一句话结论：按批序给出各批概括，并说明这是分段的结论。"""
    parts = [_text(payload.get("one_line")) for payload in payloads]
    parts = [text for text in parts if text]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    joined = "；".join(f"第 {index} 段：{text}" for index, text in enumerate(parts, start=1))
    return f"（本场转写分批分析）{joined}"


def _text(value: Any) -> str:
    """取值转展示文本；非字符串一律按空处理（模型偶尔给对象）。"""
    return value.strip() if isinstance(value, str) else ""


def _most_conservative(payloads: list[dict[str, Any]]) -> str:
    order = {LEVEL_FULL: 0, LEVEL_PARTIAL: 1, LEVEL_LIMITED: 2}
    levels = [_text(payload.get("analysis_level")) or LEVEL_PARTIAL for payload in payloads]
    return max(levels, key=lambda level: order.get(level, 1))


def _adjust_level(payload: dict[str, Any], summary) -> tuple[str, str]:
    """按真实覆盖面校正分析等级，返回（等级, 补充说明）。

    模型只看得到转写文本，看不到「有多少片段没识别成功」，因此定级与定级依据都不能由它
    单方面说了算：**依据一律由程序按真实覆盖面重写**，并把理由写进结论。
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


def _load_state(task: Task) -> tuple[dict[str, Any], int]:
    """读取分轮产物与调用次数；内容损坏时按「没有任何产物」处理。"""
    raw = task.review_passes_json
    if not raw:
        return {}, 0
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("任务 %s 的分轮产物无法解析，按全部重跑处理", task.id)
        return {}, 0
    if not isinstance(data, dict):
        return {}, 0
    passes = data.get(_STATE_PASSES)
    calls = data.get(_STATE_CALLS)
    return (passes if isinstance(passes, dict) else {}), (calls if isinstance(calls, int) else 0)


def _save_state(db: Session, task: Task, stored: dict[str, Any], calls: int) -> None:
    """每完成一轮就落库一次：失败隔离靠的就是「上一轮的产物已经在库里」。"""
    task.review_passes_json = json.dumps(
        {_STATE_CALLS: calls, _STATE_PASSES: stored}, ensure_ascii=False
    )
    task.review_batch_count = calls
    task.updated_at = utcnow()
    db.commit()


def _store(
    db: Session,
    task: Task,
    stored: dict[str, Any],
    calls: int,
    payload: dict[str, Any],
) -> None:
    """最终落库：整场结论 + 各轮产物 + 调用次数。"""
    task.review_result_json = json.dumps(payload, ensure_ascii=False)
    task.review_passes_json = json.dumps(
        {_STATE_CALLS: calls, _STATE_PASSES: stored}, ensure_ascii=False
    )
    task.review_batch_count = calls
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
    """任务级终态落库；`keep_counts` 为真时保留本次已完成的轮次与调用次数。"""
    task.review_status = status
    task.review_error = error
    task.review_progress = PROGRESS_REVIEW_DONE if status == REVIEW_STATUS_FAILED else 0
    task.review_finished_at = utcnow()
    if not keep_counts:
        task.review_segment_count = 0
        task.review_clip_count = 0
        task.review_batch_count = 0
        task.review_passes_json = None
    task.updated_at = utcnow()
    db.commit()


def reset_task_review(db: Session, task_id: str) -> Task | None:
    """重跑前清空上一轮结论**与分轮产物**：避免「上一次成功、这一次失败」却仍展示旧结论。

    清分轮产物是刻意与自动补跑区分开的：用户点「重新复盘」要的是一份全新的结论，因此
    「已完成哪几轮」这件事也必须归零；而自动补跑走的是 `mark_review_running`，那里刻意
    不清产物，以便只重跑失败的那一轮。
    """
    task = db.get(Task, task_id)
    if task is None:
        return None
    task.review_status = REVIEW_STATUS_PENDING
    task.review_error = None
    task.review_progress = 0
    task.review_segment_count = 0
    task.review_clip_count = 0
    task.review_batch_count = 0
    task.review_passes_json = None
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
    编号引用（心法 / 话术样板）在文末附一张对照表——报告会被带出系统讨论，读的人手里没有
    界面可以点开编号。
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
    lines.append(f"- 模型调用：{payload.get('batch_count', task.review_batch_count)} 次（分轮专项分析）")
    if model_name:
        lines.append(f"- 模型：{model_name}")
    lines.append("")

    if payload.get("one_line"):
        lines.extend(["## 本场一句话结论", "", payload["one_line"], ""])

    if payload.get("topic_distribution"):
        lines.extend(["## 内容主题分布", "", payload["topic_distribution"], ""])

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
            codes = _format_codes(finding.get("principle_codes"))
            suffix = f"｜{codes}" if codes else ""
            lines.append(
                f"- **{finding.get('dimension', '')}**｜{finding.get('judgement', '')}"
                f"（{finding.get('evidence_level', '')}，{stamp}）{suffix}"
            )
            if finding.get("evidence"):
                lines.append(f"  - 依据：{finding['evidence']}")
            if finding.get("suggestion") and finding["suggestion"] != "保持":
                lines.append(f"  - 建议：{finding['suggestion']}")
        lines.append("")

    repetition = payload.get("repetition") or []
    if repetition:
        lines.extend(["## 内容重复度", ""])
        for item in repetition:
            verdict = "有信息增量" if item.get("has_increment") else "同质重复"
            lines.append(
                f"- {item.get('item', '')}：共 {item.get('count', 0)} 次（{verdict}）"
                f"｜{item.get('verdict', '')}"
            )
            if item.get("times"):
                lines.append(f"  - 时段：{item['times']}")
        lines.append("")

    violations = payload.get("violations") or []
    if violations:
        lines.extend(["## 违规表达与替换", ""])
        for item in violations:
            start = item.get("start_seconds")
            stamp = format_timestamp(float(start)) if isinstance(start, (int, float)) else "时间未给出"
            lines.append(
                f"- [{stamp}] 「{item.get('term', '')}」→ {item.get('replacement', '')}"
            )
            if item.get("quote"):
                lines.append(f"  - 原话：{item['quote']}")
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
            codes = _format_codes(issue.get("principle_codes"), issue.get("script_codes"))
            if codes:
                lines.append(f"   - 引用：{codes}")
        lines.append("")

    actions = payload.get("next_actions") or []
    if actions:
        lines.extend(["## 下一场实验动作", ""])
        for position, action in enumerate(actions, start=1):
            lines.append(f"{position}. 目标：{action.get('goal', '')}")
            for label, key in (
                ("当前方式", "current_approach"),
                ("怎么做", "how"),
                ("为什么换方向", "change_reason"),
                ("观察", "observe"),
                ("成功标准", "success_criteria"),
            ):
                if action.get(key):
                    lines.append(f"   - {label}：{action[key]}")
            codes = _format_codes(action.get("script_codes"))
            if codes:
                lines.append(f"   - 引用：{codes}")
        lines.append("")

    quotes = payload.get("quotes") or []
    if quotes:
        lines.extend(["## 本场金句收录", ""])
        for quote in quotes:
            start = quote.get("start_seconds")
            stamp = format_timestamp(float(start)) if isinstance(start, (int, float)) else "时间未给出"
            lines.append(f"- [{stamp}]（{quote.get('category', '')}）{quote.get('text', '')}")
            if quote.get("scene"):
                lines.append(f"  - 适用场景：{quote['scene']}")
            if quote.get("effect"):
                lines.append(f"  - 好在哪：{quote['effect']}")
        lines.append("")

    missing = payload.get("missing_info") or []
    if missing:
        lines.extend(["## 本场不足以判断", ""])
        lines.extend(f"- {item}" for item in missing)
        lines.append("")

    reference = _reference_table(payload)
    if reference:
        lines.extend(["## 引用的心法与话术样板", ""])
        lines.extend(reference)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _format_codes(mindsets: Any, samples: Any = None) -> str:
    """把编号列成一行可读文本；两列都空时返回空串。"""
    parts: list[str] = []
    if isinstance(mindsets, list) and mindsets:
        parts.append("心法：" + "、".join(str(item) for item in mindsets))
    if isinstance(samples, list) and samples:
        parts.append("话术样板：" + "、".join(str(item) for item in samples))
    return "；".join(parts)


def _reference_table(payload: dict[str, Any]) -> list[str]:
    """文末对照表：只列本场真的引用到的编号，附原文。

    报告会被带出系统讨论（转发、打印、贴进会议），读的人手里没有界面可以点开编号。因此
    编号必须在报告内部自洽——否则「使用话术样板 04」对收件人就是一句无意义的暗语。
    """
    mindset_codes: list[str] = []
    sample_codes: list[str] = []

    def collect(container: dict[str, Any]) -> None:
        for code in container.get("principle_codes") or []:
            if isinstance(code, str) and code not in mindset_codes:
                mindset_codes.append(code)
        for code in container.get("script_codes") or []:
            if isinstance(code, str) and code not in sample_codes:
                sample_codes.append(code)

    for field in ("findings", "top_issues", "next_actions"):
        for item in payload.get(field) or []:
            if isinstance(item, dict):
                collect(item)

    lines: list[str] = []
    if mindset_codes:
        by_code = {item.code: item for item in MIND_SETS}
        lines.append("**心法**")
        for code in mindset_codes:
            item = by_code.get(code)
            if item is not None:
                lines.append(f"- {item.code}｜{item.name}：{item.check}")
            else:
                lines.append(f"- {code}：（准则中无此编号）")
    if sample_codes:
        lines.append("")
        lines.append("**话术样板**")
        for code in sample_codes:
            item = sample_by_code(code)
            if item is not None:
                label = f"（{item.usage}）" if item.usage else ""
                lines.append(f"- {item.code}{label}：{item.text}")
            else:
                lines.append(f"- {code}：（准则中无此编号）")
    return lines


__all__ = [
    "PROGRESS_REVIEW_DONE",
    "PROGRESS_REVIEW_END",
    "PROGRESS_REVIEW_START",
    "load_review_result",
    "merge_passes",
    "reset_task_review",
    "review_to_markdown",
    "run_review",
]
