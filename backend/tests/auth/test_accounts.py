"""账号管理接口（V0.5.2）：权限边界、建号的角色、删号的保护与会话作废。

这一组测的是**接口层**的契约。数据层（`store.create_user` / `delete_user` 等）的单测
在 `test_foundation.py`，这里不重复。
"""

from __future__ import annotations

import pytest

from app.models import ROLE_ADMIN, ROLE_MEMBER
from tests.conftest import (
    MEMBER_DISPLAY_NAME,
    MEMBER_PASSWORD,
    MEMBER_USERNAME,
    TEST_PASSWORD,
    TEST_USERNAME,
)

NEW_USERNAME = "fresh-account"
NEW_PASSWORD = "fresh-password-1"


def _create(client, username=NEW_USERNAME, password=NEW_PASSWORD, **extra):
    payload = {"username": username, "password": password, **extra}
    return client.post("/api/accounts", json=payload)


# ---- 登录响应里的角色 ----


def test_session_reports_role(anonymous_client):
    """前端靠这个字段决定要不要显示账号管理入口，登录与会话查询必须都带上。"""
    from tests.conftest import login

    resp = anonymous_client.get("/api/auth/session")
    assert resp.status_code == 401  # 未登录

    login(anonymous_client)
    resp = anonymous_client.get("/api/auth/session")
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == ROLE_ADMIN


def test_member_session_reports_member_role(member_client):
    resp = member_client.get("/api/auth/session")
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == ROLE_MEMBER


# ---- 权限边界 ----


def test_admin_can_list_accounts(client):
    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    usernames = [item["username"] for item in resp.json()["items"]]
    assert TEST_USERNAME in usernames


def test_member_is_forbidden_not_unauthorized(member_client):
    """403 而不是 401：他是登录用户，只是没权限。回 401 会被前端当成会话失效而弹回登录页，
    重新登录后依然没权限，形成死循环。"""
    assert member_client.get("/api/accounts").status_code == 403
    assert _create(member_client).status_code == 403
    assert member_client.delete("/api/accounts/1").status_code == 403


@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_forbidden_detail_is_chinese(member_client, method):
    """失败原因直接展示给使用者，必须是中文文案。"""
    path = "/api/accounts" if method != "delete" else "/api/accounts/1"
    kwargs = {"json": {"username": "x", "password": "y-password-1"}} if method == "post" else {}
    resp = getattr(member_client, method)(path, **kwargs)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "只有管理员账号可以执行此操作"


# ---- 建号 ----


def test_created_account_is_member(client):
    """界面建的账号一律是普通账号：请求体里根本没有角色字段，服务端写死。"""
    resp = _create(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == NEW_USERNAME
    assert body["role"] == ROLE_MEMBER
    assert body["id"] > 0
    assert body["last_login_at"] is None


def test_create_does_not_leak_password_hash(client):
    """响应体里不能出现口令哈希，哪怕是以别的字段名。"""
    body = _create(client, username="no-leak").text
    assert "pbkdf2" not in body
    assert "password_hash" not in body


def test_create_rejects_duplicate_username(client):
    assert _create(client, username="dup-user").status_code == 201
    resp = _create(client, username="dup-user")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "该账号已存在，请换一个"


def test_create_rejects_existing_admin_username(client):
    """与既有管理员重名同样要拒，不能覆盖或改坏那个账号。"""
    resp = _create(client, username=TEST_USERNAME)
    assert resp.status_code == 400


def test_create_rejects_short_password(client):
    resp = _create(client, username="short-pw", password="abc")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "密码过短：至少 8 个字符"


def test_create_requires_username(client):
    resp = _create(client, username="   ")
    assert resp.status_code == 400


def test_create_defaults_display_name_to_username(client):
    body = _create(client, username="no-display-name").json()
    assert body["display_name"] == "no-display-name"


def test_created_account_can_log_in(client):
    """端到端：界面建的号拿得到登录态，且是普通账号。

    登录动作放在**另建的客户端**上：在 `client` 上登录会让新账号的 Cookie 覆盖掉管理员
    那一张，这条用例后面的断言就都在验证另一个身份了。`client` 只用来建号。
    """
    from fastapi.testclient import TestClient

    from tests.conftest import login

    _create(client, username="can-login", password="can-login-pw-1")

    with TestClient(client.app) as fresh:
        login(fresh, "can-login", "can-login-pw-1")
        assert fresh.get("/api/auth/session").json()["user"]["role"] == ROLE_MEMBER


# ---- 删号 ----


def test_admin_can_delete_member(client, member_client):
    db_id = _find_id(client, MEMBER_USERNAME)
    resp = client.delete(f"/api/accounts/{db_id}")
    assert resp.status_code == 204
    usernames = [item["username"] for item in client.get("/api/accounts").json()["items"]]
    assert MEMBER_USERNAME not in usernames


def test_deleted_account_is_kicked_offline(client, member_client, db_session_factory):
    """删号即踢下线：旧令牌立刻失效，而不是等它自然过期。"""
    db_id = _find_id(client, MEMBER_USERNAME)
    assert member_client.get("/api/accounts").status_code == 403  # 删前是登录态

    client.delete(f"/api/accounts/{db_id}")

    assert member_client.get("/api/auth/session").status_code == 401


class _FakeUser:
    """只为读一个 id，不触碰数据库。"""

    def __init__(self, user_id: int):
        self.id = user_id


def test_delete_removes_session_rows(client, db_session_factory):
    """会话行必须真的从库里消失，不能只让令牌查不到。"""
    from app.auth import store

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        assert store.list_user_sessions(db, user), "管理员自己应有会话"
        # 先造一个 member 并让它登录，确认删号时清掉的是**它**的会话
        member = store.create_user(
            db, username="session-check", password="session-check-1", role=ROLE_MEMBER
        )
        member_id = member.id
    finally:
        db.close()

    # 让这个账号登录一次，制造出「删除前它确实持有会话」的前提。
    # 同样要另建客户端：借用 `client` 会把管理员的 Cookie 覆盖掉，后面那次删除就不是
    # 管理员发起的了
    from fastapi.testclient import TestClient

    with TestClient(client.app) as member_session:
        member_session.post(
            "/api/auth/login",
            json={"username": "session-check", "password": "session-check-1"},
        )
        db = db_session_factory()
        try:
            member = store.get_user_by_id(db, member_id)
            assert len(store.list_user_sessions(db, member)) == 1
        finally:
            db.close()

    assert client.delete(f"/api/accounts/{member_id}").status_code == 204

    db = db_session_factory()
    try:
        # 账号与它的会话都不在了
        assert store.get_user_by_id(db, member_id) is None
    finally:
        db.close()


def test_delete_rejects_admin_account(client, test_user):
    """管理员账号一律不可删，按角色判定——`init_admin --username` 可以建出任意名字的
    管理员账号，按用户名硬编码会漏掉它们。"""
    resp = client.delete(f"/api/accounts/{test_user.id}")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "管理员账号不能删除"


def test_delete_rejects_every_admin_not_just_the_first(client, db_session_factory):
    """再建一个管理员账号，它同样不可删：保护的是角色，不是某一个 id。"""
    from app.auth import store

    db = db_session_factory()
    try:
        second = store.create_user(
            db, username="second-admin", password="second-admin-1", role=ROLE_ADMIN
        )
        second_id = second.id
    finally:
        db.close()

    assert client.delete(f"/api/accounts/{second_id}").status_code == 400


def test_delete_unknown_id_is_404(client):
    resp = client.delete("/api/accounts/999999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "账号不存在"


def _find_id(client, username: str) -> int:
    for item in client.get("/api/accounts").json()["items"]:
        if item["username"] == username:
            return int(item["id"])
    raise AssertionError(f"账号 {username} 不在清单里")
