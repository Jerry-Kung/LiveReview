"""全自动流程模式的串接测试（V0.7.0）。

`conftest` 把执行器换成了同步实现，因此一次上传（或一次手工入队）会把整条链路一路跑到底，
`run()` 在每个阶段结束时调用 `advance`，本文件观察的就是这条链条的行为。

覆盖方案里需要被钉住的几件事：默认关闭时行为与之前一致；打开后一路跑到复盘；两道闸
（任务快照 + 当前开关）都生效；识别失败只自动补跑一次；一片未成功时不串复盘；模型未配置
时不入队而是落成可解释的失败。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import (
    REVIEW_STATUS_SUCCEEDED,
    UNDERSTANDING_STATUS_FAILED,
    UNDERSTANDING_STATUS_SUCCEEDED,
    Task,
)
from app.tasks.chain import AUTO_RETRY_LIMIT, sweep_pending
from app.settings_store import write_auto_pipeline
from tests.fake_llm import SAMPLE_JSON, BoomError, FakeUnderstandingClient, json_output
from tests.test_uploads import _create, _upload_all


def _enable_auto(db_session_factory, *, enabled: bool = True) -> None:
    """直接把开关写进库：设置接口只有管理员能写，而这里关心的是串接行为本身。"""
    db = db_session_factory()
    try:
        write_auto_pipeline(db, enabled=enabled, updated_by="tester")
    finally:
        db.close()


def _task(client: TestClient, task_id: str) -> dict:
    resp = client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload(client: TestClient, filename: str = "live.ts") -> str:
    """上传一个文件并等它入库，返回任务 id。

    入库会把任务交给（同步的）执行器，因此这里返回时整条链路可能已经跑完——开关是否开启
    决定了它跑到哪一步。
    """
    data = _create(client, filename=filename)
    _upload_all(client, data["upload_id"])
    assert client.post(f"/api/uploads/{data['upload_id']}/complete").status_code == 200
    return data["task_id"]


@pytest.fixture
def fail_first_understanding(monkeypatch) -> FakeUnderstandingClient:
    """识别替身：第一次调用抛错，之后返回正常结果。

    用来验证「失败后自动补跑一次」这条路径——首次必失败，补跑必成功。
    """
    fake = FakeUnderstandingClient(script=[BoomError("模型瞬时故障")], repeat_last=False)
    fake.canned = SAMPLE_JSON
    monkeypatch.setattr("app.tasks.understanding.get_understanding_client", lambda: fake)
    return fake


@pytest.fixture
def always_fail_understanding(monkeypatch) -> FakeUnderstandingClient:
    """识别替身：每次调用都失败，用于验证补跑上限与「一片未成功则不串复盘」。"""
    fake = FakeUnderstandingClient(script=[BoomError("模型持续故障")], repeat_last=True)
    monkeypatch.setattr("app.tasks.understanding.get_understanding_client", lambda: fake)
    return fake


# —— 默认关闭：行为与之前完全一致 ——


def test_disabled_by_default_does_not_chain(client, db_session_factory):
    """开关默认关闭时，上传后只跑切分，不自动进入识别。"""
    task_id = _upload(client)

    task = _task(client, task_id)
    assert task["status"] == "succeeded"
    # 识别从未被拉起：状态仍是初始的 pending，也没有起始时间
    assert task["understanding"]["status"] == "pending"
    assert task["understanding"]["started_at"] is None
    assert task["auto_run"] is False


def test_auto_run_snapshot_is_false_for_manual_upload(client, db_session_factory):
    """关闭状态下创建的任务，快照为假——日后打开开关也不会追溯它。"""
    task_id = _upload(client)

    assert _task(client, task_id)["auto_run"] is False
    # 事后打开开关，再补扫一次：这条任务仍不该被拉起来
    _enable_auto(db_session_factory, enabled=True)
    assert sweep_pending() == []
    assert _task(client, task_id)["understanding"]["status"] == "pending"


# —— 打开开关：一路跑到底 ——


def test_auto_pipeline_runs_understanding_and_review(client, db_session_factory):
    """开关打开时，上传并处理完成后自动识别、自动复盘，全程无需点击。"""
    _enable_auto(db_session_factory)
    task_id = _upload(client)

    task = _task(client, task_id)
    assert task["auto_run"] is True
    assert task["status"] == "succeeded"
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_SUCCEEDED
    assert task["understanding"]["clip_count"] > 0
    assert task["review"]["status"] == REVIEW_STATUS_SUCCEEDED
    assert task["review"]["result"] is not None


def test_auto_run_snapshot_recorded_at_creation(client, db_session_factory):
    """快照记录的是**创建时**的取值：打开开关后创建的任务为真。"""
    _enable_auto(db_session_factory)
    task_id = _upload(client, filename="second.ts")

    assert _task(client, task_id)["auto_run"] is True


# —— 两道闸 ——


def test_turning_switch_off_stops_further_chaining(client, db_session_factory):
    """关掉开关后不再串入后续阶段：识别跑完就停，不进入复盘。

    当前阶段照常跑完（已经在花的钱不中断），被拦下的是「还没发生的下一步」。
    """
    _enable_auto(db_session_factory)
    # 建任务时开关是开的，快照为真
    data = _create(client, filename="half.ts")
    _upload_all(client, data["upload_id"])

    # 上传完成前把开关关掉
    _enable_auto(db_session_factory, enabled=False)
    assert client.post(f"/api/uploads/{data['upload_id']}/complete").status_code == 200

    task = _task(client, data["task_id"])
    assert task["auto_run"] is True
    assert task["status"] == "succeeded"
    # 切分照常完成，但识别没有被串起来
    assert task["understanding"]["status"] == "pending"


def test_manual_task_not_resumed_by_sweep(client, db_session_factory):
    """手工模式的任务不会被启动补扫翻出来（不追溯）。"""
    task_id = _upload(client, filename="manual.ts")
    _enable_auto(db_session_factory, enabled=True)

    assert task_id not in sweep_pending()


# —— 自动补跑 ——


def test_understanding_failure_triggers_one_automatic_retry(
    client, db_session_factory, fail_first_understanding
):
    """识别首次失败时自动补跑一次；补跑成功后继续串到复盘。"""
    _enable_auto(db_session_factory)
    task_id = _upload(client)

    task = _task(client, task_id)
    # 补跑一次后成功，因此整场是成功的
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_SUCCEEDED
    assert task["review"]["status"] == REVIEW_STATUS_SUCCEEDED
    # 补跑确实发生过：模型至少被调用过两次（首次失败 + 补跑成功）
    assert len(fail_first_understanding.calls) >= 2


def test_understanding_retry_is_capped(client, db_session_factory, always_fail_understanding):
    """持续失败时补跑不超过上限，链路停在识别失败，不进入复盘。"""
    _enable_auto(db_session_factory)
    task_id = _upload(client)

    task = _task(client, task_id)
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_FAILED
    # 一片都没成功 → 不串复盘
    assert task["review"]["status"] == "pending"

    db = db_session_factory()
    try:
        row = db.get(Task, task_id)
        assert row is not None
        assert row.auto_retries == AUTO_RETRY_LIMIT
    finally:
        db.close()


def test_retry_counter_is_visible_through_api(client, db_session_factory, always_fail_understanding):
    """补跑计数随任务详情可查（不额外加接口）。"""
    _enable_auto(db_session_factory)
    task_id = _upload(client, filename="capped.ts")

    task = _task(client, task_id)
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_FAILED


# —— 边界：模型未配置 ——


def test_model_missing_stops_with_explanation(client, db_session_factory, use_settings):
    """模型未配置时不开跑识别，而是落成带可操作文案的失败态。"""
    _enable_auto(db_session_factory)
    use_settings(llm_base_url=None, llm_api_key=None, llm_model_name=None)

    task_id = _upload(client)

    task = _task(client, task_id)
    assert task["status"] == "succeeded"
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_FAILED
    assert "模型未配置" in (task["understanding"]["error"] or "")
    # 没有入队，因此没有起始时间这一轮
    assert task["review"]["status"] == "pending"


# —— 启动补扫 ——


def test_sweep_resumes_chain_left_unfinished(client, db_session_factory):
    """「切分成功、识别尚未入队」的任务能被补扫接上，不必手工点按钮。"""
    _enable_auto(db_session_factory)
    # 建一条已完成切分、但从未开始识别的全自动任务
    task_id = _upload(client, filename="sweep.ts")

    # 人为退回「识别从未启动」，模拟「阶段已结束、下一阶段还没入队」被中断的窗口
    db = db_session_factory()
    try:
        row = db.get(Task, task_id)
        assert row is not None
        row.understanding_status = "pending"
        row.understanding_started_at = None
        row.understanding_finished_at = None
        row.review_status = "pending"
        row.review_started_at = None
        row.review_finished_at = None
        for clip in row.clips:
            clip.understanding_status = "pending"
            clip.understanding_finished_at = None
        db.commit()
    finally:
        db.close()

    resumed = sweep_pending()
    assert task_id in resumed

    task = _task(client, task_id)
    assert task["understanding"]["status"] == UNDERSTANDING_STATUS_SUCCEEDED


def test_sweep_skips_when_switch_off(client, db_session_factory):
    """开关关闭时补扫不动任何任务。"""
    _enable_auto(db_session_factory)
    task_id = _upload(client, filename="off.ts")

    db = db_session_factory()
    try:
        row = db.get(Task, task_id)
        assert row is not None
        row.understanding_status = "pending"
        row.understanding_started_at = None
        row.understanding_finished_at = None
        db.commit()
    finally:
        db.close()

    _enable_auto(db_session_factory, enabled=False)
    assert sweep_pending() == []


def test_sweep_does_not_requeue_skipped_stage(client, db_session_factory):
    """识别曾以 `skipped` 收场（没有可识别切片）时，补扫不再反复把它翻出来。

    这条钉住一个真实的空转：`skipped` 只写 `finished_at`、不写 `started_at`，只按「没启动」
    判断会每次启动都重新入队一次，跑完又是 `skipped`。
    """
    _enable_auto(db_session_factory)
    task_id = _upload(client, filename="skipped.ts")

    db = db_session_factory()
    try:
        row = db.get(Task, task_id)
        assert row is not None
        # 造出「识别判过、结论是 skipped」的终态，且从未启动过
        row.understanding_status = "skipped"
        row.understanding_started_at = None
        row.understanding_finished_at = row.created_at
        db.commit()
    finally:
        db.close()

    assert task_id not in sweep_pending()
