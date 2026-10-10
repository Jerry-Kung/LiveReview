"""设置接口的测试（V0.7.0）：读对所有登录账号开放，写只对管理员。

这一组只覆盖对外契约与权限边界：谁能读、谁能写、写入是否真的落库并立即对新上传生效。
串接行为本身在 `test_chain.py`，两处分开是因为失败时的排查方向完全不同——这里是
「接口有没有把话说清楚」，那里是「链条有没有接上」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Task
from app.settings_store import read_auto_pipeline
from tests.test_uploads import _create


@pytest.mark.real_default
def test_read_defaults_to_enabled(client: TestClient):
    """未写过时开关为开启（V0.7.1）：新装环境不必先点开关，上传完就会自己跑完。

    标 `real_default` 跳过 conftest 里那条预写「关闭」的夹具——本用例要验的正是空表时的
    取值，夹具会把它盖掉。
    """
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_pipeline"] is True
    # 只读运行参数一并给出：它们决定「打开开关意味着什么」
    assert body["split_max_duration_seconds"] > 0
    assert body["split_max_clip_bytes"] > 0
    assert body["video_ttl_seconds"] > 0
    # 测试配置里模型是配好的
    assert body["llm_configured"] is True
    assert body["model_name"] == "test-model"


def test_member_can_read(client: TestClient, member_client: TestClient):
    """普通账号读得到，且读到的是同一个取值：上传页要据此切换说明文案。"""
    assert client.patch("/api/settings", json={"auto_pipeline": True}).status_code == 200

    resp = member_client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_pipeline"] is True


def test_member_cannot_write(client: TestClient, member_client: TestClient):
    """普通账号写入被拒，且是 403（已登录、只是不对他开放），不是 401。"""
    assert client.patch("/api/settings", json={"auto_pipeline": True}).status_code == 200

    resp = member_client.patch("/api/settings", json={"auto_pipeline": False})
    assert resp.status_code == 403, resp.text
    # 拒绝之后开关不该有任何变化
    assert client.get("/api/settings").json()["auto_pipeline"] is True


def test_anonymous_cannot_read(anonymous_client: TestClient):
    """未登录读不到。"""
    resp = anonymous_client.get("/api/settings")
    assert resp.status_code == 401


def test_admin_write_persists_and_returns_stored_value(client: TestClient, db_session_factory):
    """管理员写入后落库，响应回读的是真正落库的取值。"""
    resp = client.patch("/api/settings", json={"auto_pipeline": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_pipeline"] is True

    # 再读一次确认不是只改了响应
    assert client.get("/api/settings").json()["auto_pipeline"] is True

    db = db_session_factory()
    try:
        assert read_auto_pipeline(db) is True
    finally:
        db.close()

    # 关回去
    assert client.patch("/api/settings", json={"auto_pipeline": False}).json()["auto_pipeline"] is False


def test_write_applies_to_new_uploads_only(client: TestClient, db_session_factory):
    """写入只影响此后新建的任务：已有任务的快照不变。"""
    # 先建一条手工模式的任务
    manual = _create(client, filename="before.ts")
    db = db_session_factory()
    try:
        row = db.get(Task, manual["task_id"])
        assert row is not None and row.auto_run is False
    finally:
        db.close()

    assert client.patch("/api/settings", json={"auto_pipeline": True}).status_code == 200

    # 老任务的快照不受影响
    db = db_session_factory()
    try:
        row = db.get(Task, manual["task_id"])
        assert row is not None and row.auto_run is False
    finally:
        db.close()

    # 新建的任务带上新取值
    fresh = _create(client, filename="after.ts")
    db = db_session_factory()
    try:
        row = db.get(Task, fresh["task_id"])
        assert row is not None and row.auto_run is True
    finally:
        db.close()


def test_write_records_operator(client: TestClient, db_session_factory):
    """记下最后修改者：全自动会真实花钱，出问题时需要能追溯是谁打开的。"""
    assert client.patch("/api/settings", json={"auto_pipeline": True}).status_code == 200

    from app.models import AppSetting, SETTINGS_ROW_ID

    db = db_session_factory()
    try:
        row = db.get(AppSetting, SETTINGS_ROW_ID)
        assert row is not None
        assert row.updated_by == "tester"
    finally:
        db.close()


def test_task_response_exposes_auto_run(client: TestClient, db_session_factory):
    """任务详情带上快照字段，供详情页标注与费用追溯。"""
    data = _create(client, filename="snap.ts")
    task = client.get(f"/api/tasks/{data['task_id']}").json()
    assert task["auto_run"] is False
