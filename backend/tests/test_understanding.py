"""V0.2 识别链路的接口测试：模型用假客户端，存储用 Mock，执行器同步执行。

覆盖 V0.2 的四条硬要求：切片逐片识别并带原视频时间、单片失败可单独重试、已完成的
片段与识别结果可复用、整场结果可汇集查看。识别质量本身不在自动化测试范围内。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.llm.base import LLMServerError, LLMTimeoutError
from app.models import UNDERSTANDING_STATUS_FAILED, UNDERSTANDING_STATUS_SUCCEEDED
from tests.fake_llm import SAMPLE_JSON, BoomError, FakeUnderstandingClient, json_output
from tests.test_uploads import _create, _upload_all


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


def _clips(client: TestClient, task_id: str) -> list[dict]:
    return _task(client, task_id)["clips"]


@pytest.fixture
def model(monkeypatch) -> FakeUnderstandingClient:
    """注入假模型客户端并返回它，供断言调用次数与入参。

    识别链路用 `from app.llm import ...` 直接绑定函数名，因此补丁要打在**使用点**上
    （`app.tasks.understanding`），打在 `app.llm` 上不会生效。
    """
    fake = FakeUnderstandingClient()
    monkeypatch.setattr("app.tasks.understanding.get_understanding_client", lambda: fake)
    return fake


def set_script(model: FakeUnderstandingClient, *items, repeat_last: bool = False) -> None:
    """设置假客户端的返回脚本（按调用顺序消耗）。"""
    model.script = list(items)
    model.repeat_last = repeat_last
    model._queue = list(items)


@pytest.fixture
def multi_clip_client(client, test_settings, monkeypatch):
    """把单片时长压到 300 秒：假素材 600 秒，从而切出多片，便于验证跨片段对齐。

    只在本文件里调整，不动全局测试配置——其它用例的切分预期建立在默认参数上。
    """
    monkeypatch.setattr(
        "app.tasks.runner.get_settings",
        lambda: test_settings.model_copy(update={"split_max_duration_seconds": 300}),
    )
    return client


# —— 整场识别 ——


def test_understanding_runs_over_all_clips_and_aligns_time(multi_clip_client, model):
    """逐片识别，条目时间落回原视频时间轴：片段起点 + 片段内相对时间。"""
    client = multi_clip_client
    task_id = _prepare(client)
    before = _clips(client, task_id)
    assert len(before) >= 2

    resp = client.post(f"/api/tasks/{task_id}/understanding")
    assert resp.status_code == 202, resp.text

    task = _task(client, task_id)
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_SUCCEEDED
    assert task["understanding"]["clip_count"] == len(before)
    assert task["understanding"]["segment_count"] == 2 * len(before)
    assert task["understanding"]["finished_at"] is not None
    assert task["understanding"]["model_name"] == "test-model"
    assert all(clip["understanding_status"] == UNDERSTANDING_STATUS_SUCCEEDED for clip in task["clips"])

    transcript = client.get(f"/api/tasks/{task_id}/transcript").json()
    assert transcript["segment_count"] == 2 * len(before)

    # 第一片的第二条记录：起始时间应为「该片起点 + 4.0 秒」
    first_clip = before[0]
    first_segment = next(item for item in transcript["segments"] if item["clip_index"] == first_clip["index"])
    assert first_segment["start_seconds"] == pytest.approx(first_clip["start_seconds"] + 0.5, abs=0.01)
    assert first_segment["content"] == "欢迎来到直播间"
    # 相对时间同时保留，便于对照模型原始输出
    assert first_segment["clip_start_seconds"] == pytest.approx(0.5, abs=0.01)


def test_understanding_uses_presigned_url_and_built_prompt(client, model):
    """发给模型的是预签名 HTTPS 地址与拼好的提示词（题面 + 输出格式约束）。"""
    task_id = _prepare(client)
    client.post(f"/api/tasks/{task_id}/understanding")

    assert len(model.calls) == len(_clips(client, task_id))
    for call in model.calls:
        assert call["video_url"].startswith("https://mock-tos.local/")
        assert "clip" in call["video_url"]
        assert "人声语音" in call["prompt"]
        assert "start_time" in call["prompt"]


def test_understanding_can_run_twice_without_repeating_successful_clips(multi_clip_client, model):
    """重跑不重复请求已成功的片段，也不影响已有结果。"""
    client = multi_clip_client
    task_id = _prepare(client)
    client.post(f"/api/tasks/{task_id}/understanding")
    first_round = len(model.calls)

    resp = client.post(f"/api/tasks/{task_id}/understanding")
    assert resp.status_code == 202
    assert len(model.calls) == first_round  # 全部已成功，没有新增请求

    task = _task(client, task_id)
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_SUCCEEDED
    assert task["understanding"]["clip_count"] == len(_clips(client, task_id))


# —— 失败与局部重试 ——


def test_single_clip_failure_does_not_block_others(multi_clip_client, model):
    """单片失败只影响那一片：其余片段照样识别，整场状态标为「部分失败」。"""
    client = multi_clip_client
    task_id = _prepare(client)
    # 第一片抛瞬时错误，其余沿用正常输出
    set_script(model, LLMTimeoutError("第一次超时"))

    client.post(f"/api/tasks/{task_id}/understanding")

    task = _task(client, task_id)
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_FAILED
    assert task["understanding"]["failed_clip_count"] == 1
    assert "1/" in task["understanding"]["error"]

    clips = task["clips"]
    assert clips[0]["understanding_status"] == UNDERSTANDING_STATUS_FAILED
    assert "LLMTimeoutError" in clips[0]["understanding_error"]
    assert all(clip["understanding_status"] == UNDERSTANDING_STATUS_SUCCEEDED for clip in clips[1:])


def test_failed_clip_can_be_retried_individually(client, model):
    """单片重试：只重发失败的那一片，成功后整场状态回到成功。"""
    task_id = _prepare(client)
    set_script(model, BoomError("模型返回了无法解析的内容"))
    client.post(f"/api/tasks/{task_id}/understanding")
    assert _clips(client, task_id)[0]["understanding_status"] == UNDERSTANDING_STATUS_FAILED

    calls_before = len(model.calls)
    resp = client.post(f"/api/tasks/{task_id}/clips/0/understanding")
    assert resp.status_code == 200, resp.text
    assert len(model.calls) == calls_before + 1  # 只发了一次请求

    task = _task(client, task_id)
    assert task["clips"][0]["understanding_status"] == UNDERSTANDING_STATUS_SUCCEEDED
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_SUCCEEDED
    assert task["understanding"]["failed_clip_count"] == 0


def test_retry_unknown_clip_is_rejected(client, model):
    task_id = _prepare(client)
    resp = client.post(f"/api/tasks/{task_id}/clips/999/understanding")
    assert resp.status_code == 409


def test_understanding_requires_configured_model(client, model, test_settings, monkeypatch):
    """模型未配置时明确拒绝，不进入执行——缺配置应当立即暴露，而不是逐片各失败一次。"""
    from app.config import get_settings

    unconfigured = test_settings.model_copy(
        update={"llm_base_url": None, "llm_api_key": None, "llm_model_name": None}
    )
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: unconfigured
    try:
        task_id = _prepare(client)
        resp = client.post(f"/api/tasks/{task_id}/understanding")
        assert resp.status_code == 503
        assert "LLM_API_KEY" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_settings, None)


# —— 汇总与全文 ——


def test_transcript_reports_missing_clip_explicitly(multi_clip_client, model):
    """失败片段在全文里显式留位，不被静默跳过。"""
    client = multi_clip_client
    task_id = _prepare(client)
    set_script(model, BoomError("模型侧故障"))
    client.post(f"/api/tasks/{task_id}/understanding")

    resp = client.get(f"/api/tasks/{task_id}/transcript")
    assert resp.status_code == 200
    body = resp.json()
    assert body["failed_clip_count"] == 1
    assert "识别失败" in body["text"]
    assert "模型侧故障" in body["text"]

    plain = client.get(f"/api/tasks/{task_id}/transcript.txt")
    assert plain.status_code == 200
    assert plain.headers["content-type"].startswith("text/plain")
    assert "识别失败" in plain.text


def test_transcript_is_empty_before_understanding(client, model):
    """还没识别过时不编造内容，全文给出明确的空态。"""
    task_id = _prepare(client)
    body = client.get(f"/api/tasks/{task_id}/transcript").json()
    assert body["segment_count"] == 0
    assert body["status"] == "pending"
    # 「还没识别」与「识别出来是空的」是两件不同的事，文案必须区分
    assert "整场尚未识别" in body["text"]


def test_transcript_keeps_parse_warnings(client, model):
    """被丢弃的条目在片段上留下告警，全文一并呈现。"""
    task_id = _prepare(client)
    model.canned = json_output(
        [
            {"start_time": 1, "end_time": 2, "content": "有效"},
            {"start_time": 3, "content": "缺结束时间"},
        ]
    )
    client.post(f"/api/tasks/{task_id}/understanding")

    task = _task(client, task_id)
    warnings = task["clips"][0]["understanding_warnings"]
    assert len(warnings) == 1
    assert "第 2 条" in warnings[0]

    body = client.get(f"/api/tasks/{task_id}/transcript").json()
    assert "解析告警" in body["text"]


def test_out_of_range_segment_is_flagged(multi_clip_client, model):
    """越界时间只标记，仍进入结果与全文。"""
    client = multi_clip_client
    task_id = _prepare(client)
    # 片段长度 300s，单位判定阈值 601s：600 秒是合法的秒数，只是超出了片段长度
    model.canned = json_output(
        [{"start_time": 0, "end_time": 600, "content": "时间越界的记录"}]
    )
    client.post(f"/api/tasks/{task_id}/understanding")

    body = client.get(f"/api/tasks/{task_id}/transcript").json()
    assert body["segment_count"] == len(_clips(client, task_id))
    first = body["segments"][0]
    assert first["out_of_range"] is True
    assert "时间越界" in body["text"]


def test_empty_array_result_is_not_treated_as_failure(multi_clip_client, model):
    """模型返回空数组是有效结果：片段里确实没有人声。"""
    client = multi_clip_client
    task_id = _prepare(client)
    model.canned = "[]"
    client.post(f"/api/tasks/{task_id}/understanding")

    task = _task(client, task_id)
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_SUCCEEDED
    assert task["understanding"]["segment_count"] == 0
    assert "未识别出人声语音" in client.get(f"/api/tasks/{task_id}/transcript").json()["text"]


def test_understanding_requires_clips(client, model, db_session_factory, test_settings, storage):
    """没有切片的任务不允许启动识别，而不是跑一遍空任务。"""
    from app.models import Task
    from app.models import utcnow

    db = db_session_factory()
    try:
        task = Task(id="noclips0000000001", kind="ingest", status="succeeded", object_key="k")
        db.add(task)
        db.commit()
    finally:
        db.close()

    resp = client.post("/api/tasks/noclips0000000001/understanding")
    assert resp.status_code == 409
    assert "切片" in resp.json()["detail"]


def test_server_error_keeps_request_id_in_clip_error(client, model):
    """服务端错误的 request_id 要落进片段失败原因，便于向服务商定位问题。"""
    task_id = _prepare(client)
    set_script(
        model,
        LLMServerError("模型服务返回可重试错误", status_code=500, code="InternalError", request_id="req-42"),
        repeat_last=True,
    )
    client.post(f"/api/tasks/{task_id}/understanding")

    clip = _clips(client, task_id)[0]
    assert "req-42" in clip["understanding_error"]
    assert "status_code=500" in clip["understanding_error"]


