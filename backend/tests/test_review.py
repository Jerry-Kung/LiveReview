"""V0.3 复盘链路的接口与编排测试：模型用假客户端，执行器同步执行。

覆盖本版的硬要求：整场转写作为输入、复盘节点调用一次（长直播分批）、失败片段显式警示、
结论可由接口与页面共用、重跑覆盖旧结论、状态与错误如实呈现、无输入时不发请求。
提示词措辞与结论质量本身不在自动化测试范围内。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.llm.base import LLMServerError, ReviewResult
from app.llm.parsing import normalise_review, parse_response
from app.models import (
    REVIEW_STATUS_FAILED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_RUNNING,
    REVIEW_STATUS_SKIPPED,
    REVIEW_STATUS_SUCCEEDED,
    UNDERSTANDING_STATUS_FAILED,
    UNDERSTANDING_STATUS_SUCCEEDED,
    Task,
)
from app.tasks.review import merge_reviews
from app.transcript import ClipBlock, TranscriptSummary
from tests.fake_llm import SAMPLE_REVIEW, BoomError, FakeReviewClient, json_output, review_output
from tests.test_uploads import _create, _upload_all

# 复盘结论的「下一场动作」目标文案：与 SAMPLE_REVIEW 保持一致，断言时引用同一份来源
SAMPLE_GOAL = SAMPLE_REVIEW["next_actions"][0]["goal"]


class _FakeTextResponse:
    """假返回：把文本包成 responses 接口的形态。"""

    status = "completed"
    usage = {"total_tokens": 5}

    def __init__(self, text: str) -> None:
        block = type("Block", (), {"type": "output_text", "text": text})()
        self.output = [type("Item", (), {"content": [block]})()]


class _ScriptedUnderstanding:
    """识别替身：第一片返回带语气的记录（按顺序消耗），其余片段抛脚本里的异常。

    只在本文件使用——这里的目的是**制造一个带缺口的整场**，用来验证缺口是否被如实
    传递到复盘输入与结论等级里。
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)

    def understand(self, *, video_url, prompt):
        item = self._script.pop(0) if self._script else ""
        if isinstance(item, BaseException):
            raise item
        return parse_response(_FakeTextResponse(str(item)))


def _prepare(client: TestClient, filename: str = "live.ts") -> str:
    """上传一个文件并等切分完成，返回任务 id。"""
    data = _create(client, filename=filename)
    _upload_all(client, data["upload_id"])
    assert client.post(f"/api/uploads/{data['upload_id']}/complete").status_code == 200
    return data["task_id"]


def _task(client: TestClient, task_id: str) -> dict:
    resp = client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _review(client: TestClient, task_id: str) -> dict:
    review = _task(client, task_id)["review"]
    assert review is not None
    return review


def _understood(client: TestClient, task_id: str) -> None:
    """先完成一轮识别：复盘的前置条件是「有已识别的语音记录」。"""
    resp = client.post(f"/api/tasks/{task_id}/understanding")
    assert resp.status_code == 202, resp.text


@pytest.fixture
def model(monkeypatch) -> FakeReviewClient:
    """注入假复盘客户端并返回它，供断言调用次数与入参。

    复盘链路用 `from app.llm import ...` 直接绑定函数名，因此补丁要打在**使用点**上
    （`app.tasks.review`），打在 `app.llm` 上不会生效。
    """
    fake = FakeReviewClient()
    monkeypatch.setattr("app.tasks.review.get_review_client", lambda: fake)
    return fake


def set_script(model: FakeReviewClient, *items, repeat_last: bool = False) -> None:
    """设置假客户端的返回脚本（按调用顺序消耗）。"""
    model.script = list(items)
    model.repeat_last = repeat_last
    model._queue = list(items)


def test_review_requires_understanding(client: TestClient, model: FakeReviewClient):
    """没有识别结果时拒绝启动：给出可操作的原因，而不是入队一个注定被跳过的任务。"""
    task_id = _prepare(client)

    resp = client.post(f"/api/tasks/{task_id}/review")
    assert resp.status_code == 409
    assert "识别" in resp.json()["detail"]
    assert model.calls == []


def test_review_produces_structured_result(client: TestClient, model: FakeReviewClient):
    """一次复盘调用产出结构化结论，并落进任务响应。"""
    task_id = _prepare(client)
    _understood(client, task_id)

    resp = client.post(f"/api/tasks/{task_id}/review")
    assert resp.status_code == 202, resp.text

    review = _review(client, task_id)
    assert review["status"] == REVIEW_STATUS_SUCCEEDED
    assert review["progress"] == 100
    assert review["segment_count"] > 0
    assert review["finished_at"] is not None
    assert review["model_name"] == "test-model"

    result = review["result"]
    assert result["one_line"] == SAMPLE_REVIEW["one_line"]
    assert result["analysis_level"] == "部分"
    assert [event["topic"] for event in result["key_events"]] == ["A 产品讲解", "F 权益政策"]
    assert result["findings"][0]["evidence_level"] == "事实"
    assert len(result["top_issues"]) == 1
    assert result["next_actions"][0]["goal"] == SAMPLE_GOAL
    assert result["missing_info"]


def test_review_prompt_carries_guideline_and_transcript(client: TestClient, model: FakeReviewClient):
    """送模型的两段提示词：system 是裁剪后的准则，user 是整场转写与覆盖面。"""
    task_id = _prepare(client)
    _understood(client, task_id)
    assert client.post(f"/api/tasks/{task_id}/review").status_code == 202

    assert len(model.calls) == 1
    call = model.calls[0]
    # 准则的可识别要点：业务链路、表达方式分类、证据分级、输出契约
    assert "留资" in call["system_prompt"]
    assert "参数直给" in call["system_prompt"]
    assert "高置信推断" in call["system_prompt"]
    assert "top_issues" in call["system_prompt"]
    # 不可观测的部分必须已被裁掉：没有经营数据就不该出现在准则里
    assert "巨懂车" not in call["system_prompt"]
    assert "OCR" not in call["system_prompt"]
    # 用户消息里带整场转写与片段覆盖情况
    assert "语音转写全文" in call["user_prompt"]
    assert "欢迎来到直播间" in call["user_prompt"]
    assert "片段：共" in call["user_prompt"]


def test_review_input_includes_tone_and_failed_clips(
    client: TestClient, model: FakeReviewClient, test_settings, monkeypatch
):
    """转写输入带语气标注；失败片段在输入里显式声明缺失，不让模型把缺口当成静音。"""
    # 压短单片时长以切出多片：只有多片才可能「一片成功、一片失败」
    monkeypatch.setattr(
        "app.tasks.runner.get_settings",
        lambda: test_settings.model_copy(update={"split_max_duration_seconds": 300}),
    )
    task_id = _prepare(client)
    assert len(_task(client, task_id)["clips"]) >= 2

    monkeypatch.setattr(
        "app.tasks.understanding.get_understanding_client",
        lambda: _ScriptedUnderstanding([_toned_output("这台车适合长途穿越"), BoomError("识别失败")]),
    )
    _understood(client, task_id)

    review = _review(client, task_id)
    assert review["status"] == REVIEW_STATUS_PENDING
    assert client.post(f"/api/tasks/{task_id}/review").status_code == 202

    user_prompt = model.calls[0]["user_prompt"]
    assert "识别失败" in user_prompt
    assert "不要据此外推" in user_prompt
    assert "语气：强调" in user_prompt
    # 失败片段在结论里也留了痕：缺口进了结果，不只是进了提示词
    review = _review(client, task_id)
    assert review["result"]["failed_clip_count"] > 0
    assert review["result"]["analysis_level"] == "部分"


def test_review_level_is_corrected_by_coverage():
    """缺片时由程序把等级压到「部分」：模型看不到缺口全貌，定级不能由它说了算。"""
    payload, warnings = normalise_review({**SAMPLE_REVIEW, "analysis_level": "完整"})
    assert warnings == []

    merged = merge_reviews([ReviewResult(payload=payload)], summary=_summary(failed=1, succeeded=1))
    assert merged["analysis_level"] == "部分"
    # 依据由程序按真实覆盖面重写，并保留模型自己的判断
    assert "1/2 片识别失败" in merged["level_reason"]
    # 缺口同时进入「不足以判断」清单，用户看结论时不会漏掉这一条
    assert any("1/2 片识别成功" in item for item in merged["missing_info"])

def test_review_level_downgraded_when_coverage_too_low():
    """覆盖不足时降到「受限」：只给主题与结构的观察。"""
    payload, _ = normalise_review({**SAMPLE_REVIEW, "analysis_level": "完整"})
    merged = merge_reviews([ReviewResult(payload=payload)], summary=_summary(failed=3, succeeded=1))
    assert merged["analysis_level"] == "受限"


def test_review_skipped_without_segments(client: TestClient, model: FakeReviewClient, monkeypatch):
    """识别成功但没有语音记录时判 skipped：不发请求，也不把空结论伪装成复盘成功。"""
    task_id = _prepare(client)
    monkeypatch.setattr(
        "app.tasks.understanding.get_understanding_client",
        lambda: _ScriptedUnderstanding(["[]"]),
    )
    _understood(client, task_id)

    resp = client.post(f"/api/tasks/{task_id}/review")
    assert resp.status_code == 202, resp.text

    review = _review(client, task_id)
    assert review["status"] == REVIEW_STATUS_SKIPPED
    assert review["result"] is None
    assert "识别" in review["error"]
    assert model.calls == []


def test_review_failure_records_reason(client: TestClient, model: FakeReviewClient):
    """模型侧失败时状态与原因都落库，前端能看到「这一轮没过」而不是旧结论。"""
    task_id = _prepare(client)
    _understood(client, task_id)
    set_script(model, LLMServerError("模型服务返回可重试错误", status_code=503, code="overload"))

    resp = client.post(f"/api/tasks/{task_id}/review")
    assert resp.status_code == 202, resp.text

    review = _review(client, task_id)
    assert review["status"] == REVIEW_STATUS_FAILED
    assert "LLMServerError" in review["error"]
    assert review["result"] is None


def test_review_rerun_replaces_previous_result(client: TestClient, model: FakeReviewClient):
    """重跑覆盖旧结论：换提示词后再跑一次，界面只该显示最新的那一份。"""
    task_id = _prepare(client)
    _understood(client, task_id)

    assert client.post(f"/api/tasks/{task_id}/review").status_code == 202
    first = _review(client, task_id)["result"]["one_line"]

    changed = {**SAMPLE_REVIEW, "one_line": "第二次复盘的结论"}
    set_script(model, review_output(changed))
    assert client.post(f"/api/tasks/{task_id}/review").status_code == 202

    second = _review(client, task_id)
    assert second["result"]["one_line"] == "第二次复盘的结论"
    assert second["result"]["one_line"] != first
    assert len(model.calls) == 2


def test_review_conflict_while_running(
    client: TestClient, model: FakeReviewClient, db_session_factory
):
    """复盘进行中再次启动返回 409：避免同一任务并发出两次模型调用。"""
    task_id = _prepare(client)
    _understood(client, task_id)

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        assert task is not None
        task.review_status = REVIEW_STATUS_RUNNING
        db.commit()
    finally:
        db.close()

    resp = client.post(f"/api/tasks/{task_id}/review")
    assert resp.status_code == 409
    assert "进行中" in resp.json()["detail"]
    assert model.calls == []


def test_review_batches_long_transcript(client: TestClient, model: FakeReviewClient, test_settings):
    """长转写按批次调用，结论合并后仍是一份完整结构；批次信息随结论返回。"""
    task_id = _prepare(client)

    # 把单批上限压到很小的值，让同一场识别结果必然被切成长短两批
    small_settings = test_settings.model_copy(update={"review_input_chars_per_call": 60})
    from app.tasks import review as review_module

    original = review_module.get_settings
    review_module.get_settings = lambda: small_settings  # type: ignore[assignment]
    # 两批的结论故意不同：合并要看的是「两批都进了结果」，一批的结论不该覆盖另一批
    set_script(
        model,
        {
            **SAMPLE_REVIEW,
            "one_line": "前半场结论",
            "level_reason": "前半场依据",
            "key_events": [SAMPLE_REVIEW["key_events"][0]],
        },
        {
            **SAMPLE_REVIEW,
            "one_line": "后半场结论",
            "level_reason": "后半场依据",
            "key_events": [SAMPLE_REVIEW["key_events"][1]],
        },
    )
    try:
        _understood(client, task_id)
        assert client.post(f"/api/tasks/{task_id}/review").status_code == 202
    finally:
        review_module.get_settings = original  # type: ignore[assignment]

    assert len(model.calls) > 1
    review = _review(client, task_id)
    assert review["batch_count"] == len(model.calls)
    assert review["result"]["batch_count"] > 1
    # 多批时无法保留单批的结论，合并结果给出分段的说明，且两批的原话都留了下来
    assert "分批分析" in review["result"]["one_line"]
    assert "前半场结论" in review["result"]["one_line"]
    assert "后半场结论" in review["result"]["one_line"]
    assert "前半场依据" in review["result"]["level_reason"]
    # 分批结论合并后按时间排序，且 TOP 条目仍受准则约束
    assert [event["topic"] for event in review["result"]["key_events"]] == [
        "A 产品讲解",
        "F 权益政策",
    ]
    assert len(review["result"]["top_issues"]) <= 3
    # 每批输入都声明了「这只是全场的一部分」
    assert all("分批输入" in call["user_prompt"] for call in model.calls)


def test_review_download_markdown(client: TestClient, model: FakeReviewClient):
    """Markdown 报告与页面同源：下载内容包含结论、证据与缺口说明。"""
    task_id = _prepare(client)
    _understood(client, task_id)
    assert client.post(f"/api/tasks/{task_id}/review").status_code == 202

    resp = client.get(f"/api/tasks/{task_id}/review.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    body = resp.text
    assert "直播复盘" in body
    assert SAMPLE_REVIEW["one_line"] in body
    assert "TOP 问题" in body
    assert "下一场实验动作" in body
    assert "本场不足以判断" in body


def test_review_download_rejected_before_review(client: TestClient, model: FakeReviewClient):
    """没复盘过就下载：返回 409 与当前状态，不给一份空报告。"""
    task_id = _prepare(client)
    _understood(client, task_id)

    resp = client.get(f"/api/tasks/{task_id}/review.md")
    assert resp.status_code == 409
    assert "复盘" in resp.json()["detail"]


def test_review_recovered_after_restart(client: TestClient, model: FakeReviewClient, db_session_factory):
    """进程重启打断的复盘会被退回待跑并重新入队：输入已落库，续跑不重跑识别。"""
    task_id = _prepare(client)
    _understood(client, task_id)
    understanding_calls = client.get(f"/api/tasks/{task_id}").json()["understanding"]["segment_count"]

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        assert task is not None
        task.review_status = REVIEW_STATUS_RUNNING
        task.review_finished_at = None
        task.review_error = "进程中断"
        db.commit()
    finally:
        db.close()

    from app.tasks.runner import reclaim_interrupted_review

    db = db_session_factory()
    try:
        resumed = reclaim_interrupted_review(db)
    finally:
        db.close()

    assert task_id in resumed
    review = _review(client, task_id)
    assert review["status"] == REVIEW_STATUS_PENDING
    assert review["error"] is None
    # 识别结果没被动过：复盘恢复不触碰识别
    assert _task(client, task_id)["understanding"]["segment_count"] == understanding_calls


def _toned_output(content: str) -> str:
    """一条带语气标注的识别输出：复盘输入里的语气标注来自这里。"""
    return json_output(
        [{"start_time": 1.0, "end_time": 3.0, "content": content, "tone": "强调"}]
    )


def _summary(*, succeeded: int, failed: int) -> TranscriptSummary:
    """构造一份覆盖面确定的汇总：等级兜底的用例只需要片段状态。"""
    clips = [
        ClipBlock(
            index=index,
            start_seconds=index * 10.0,
            end_seconds=(index + 1) * 10.0,
            status=UNDERSTANDING_STATUS_SUCCEEDED,
            segments=[{"content": "测试话术", "start_seconds": index * 10.0}],
        )
        for index in range(succeeded)
    ]
    clips.extend(
        ClipBlock(
            index=succeeded + index,
            start_seconds=(succeeded + index) * 10.0,
            end_seconds=(succeeded + index + 1) * 10.0,
            status=UNDERSTANDING_STATUS_FAILED,
            error="boom",
        )
        for index in range(failed)
    )
    return TranscriptSummary(
        task_id="t",
        filename="a.ts",
        status=UNDERSTANDING_STATUS_SUCCEEDED,
        model_name=None,
        clips=clips,
        segments=[],
        text="",
    )
