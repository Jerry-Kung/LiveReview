"""鉴权接口的 HTTP 契约：登录、退出、当前身份、状态，以及 Cookie 的收发。"""

from __future__ import annotations

import time

from app.auth import session as ticket
from tests.conftest import TEST_PASSWORD, TEST_SESSION_SECRET, TEST_USERNAME, hash_for_test


def _login(client, username=TEST_USERNAME, password=TEST_PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---- 登录 ----


def test_login_sets_session_cookie(anonymous_client):
    resp = _login(anonymous_client)
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == TEST_USERNAME
    assert resp.json()["user"]["display_name"] == "测试用户"

    cookie = resp.headers["set-cookie"]
    assert ticket.COOKIE_NAME in cookie
    # 脚本读不到会话票据；跨站请求默认不携带
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    # 本地开发是 http，标了 Secure 会让登录后立刻掉线
    assert "Secure" not in cookie


def test_login_rejects_wrong_password(anonymous_client):
    resp = _login(anonymous_client, password="not-the-password")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "账号或密码不正确"
    assert "set-cookie" not in resp.headers


def test_login_does_not_reveal_whether_account_exists(anonymous_client):
    """账号不存在与口令错误必须返回同一句话，否则接口成了账号探测器。"""
    unknown = _login(anonymous_client, username="nobody", password="whatever")
    wrong = _login(anonymous_client, username=TEST_USERNAME, password="whatever")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_login_username_is_trimmed(anonymous_client):
    """前后空格不该让用户以为口令错了——复制粘贴带空格是最常见的一种。"""
    assert _login(anonymous_client, username=f"  {TEST_USERNAME}  ").status_code == 200


def test_login_rejects_empty_fields(anonymous_client):
    assert _login(anonymous_client, username="", password="x").status_code == 422
    assert _login(anonymous_client, username="x", password="").status_code == 422


def test_login_is_throttled_after_repeated_failures(anonymous_client):
    for _ in range(5):
        _login(anonymous_client, password="wrong")
    resp = _login(anonymous_client)
    assert resp.status_code == 429
    assert "频繁" in resp.json()["detail"]


def test_login_reports_missing_user_configuration(anonymous_client, use_settings):
    """未配置任何账号时明确报「没配」，而不是让每次登录都变成「口令不对」。"""
    use_settings(auth_users="")
    resp = _login(anonymous_client)
    assert resp.status_code == 503
    assert "尚未配置" in resp.json()["detail"]


def test_login_reports_broken_user_configuration(anonymous_client, use_settings):
    """配置写坏时报 500 并说明是服务端配置问题，不把内部格式抛给用户。"""
    use_settings(auth_users="broken-entry")
    resp = _login(anonymous_client)
    assert resp.status_code == 500
    assert "配置无效" in resp.json()["detail"]


# ---- 当前身份 ----


def test_session_returns_current_user(client):
    resp = client.get("/api/auth/session")
    assert resp.status_code == 200
    assert resp.json()["user"] == {"username": TEST_USERNAME, "display_name": "测试用户"}


def test_session_requires_login(anonymous_client):
    # 未登录不是错误而是「还没有身份」，前端据此渲染登录页，因此这里也返回 401
    assert anonymous_client.get("/api/auth/session").status_code == 401


def test_session_rejects_forged_ticket(anonymous_client):
    anonymous_client.cookies.set(ticket.COOKIE_NAME, "forged.ticket.value")
    assert anonymous_client.get("/api/auth/session").status_code == 401


def test_session_rejects_ticket_signed_with_another_secret(anonymous_client):
    """换密钥即作废全部旧票据：这是本版「踢人」能力的实现方式。"""
    anonymous_client.cookies.set(
        ticket.COOKIE_NAME, ticket.issue("some-other-secret-value", TEST_USERNAME)
    )
    assert anonymous_client.get("/api/auth/session").status_code == 401


def test_session_rejects_expired_ticket(anonymous_client):
    stale = ticket.issue(TEST_SESSION_SECRET, TEST_USERNAME, issued_at=int(time.time()) - 13 * 3600)
    anonymous_client.cookies.set(ticket.COOKIE_NAME, stale)
    assert anonymous_client.get("/api/auth/session").status_code == 401


def test_session_rejects_ticket_of_removed_account(anonymous_client, use_settings):
    """账号从配置里摘掉后，旧票据必须立即失效，不能继续通行。"""
    use_settings(auth_users=f"someone:{hash_for_test('another-password')}")
    assert anonymous_client.get("/api/auth/session").status_code == 401


# ---- 退出 ----


def test_logout_clears_cookie_and_blocks_reuse(client):
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert client.get("/api/auth/session").status_code == 401


def test_logout_is_idempotent(anonymous_client):
    """重复退出不该报错：用户可能开了多个标签页，各自都点了退出。"""
    assert anonymous_client.post("/api/auth/logout").status_code == 204
    assert anonymous_client.post("/api/auth/logout").status_code == 204


# ---- 运行配置状态 ----


def test_status_requires_login(anonymous_client):
    assert anonymous_client.get("/api/auth/status").status_code == 401


def test_status_reports_configuration_without_secrets(client):
    data = client.get("/api/auth/status").json()
    assert data["version"] == "0.5.0"
    assert data["database"] == "ok"
    assert data["storage"] in ("configured", "not_configured", "unavailable")
    assert data["llm"] in ("configured", "not_configured")
    assert data["auth_users"] == 1
    assert data["auth_session_secret_configured"] is True
    assert data["auth_warnings"] == []
    # 状态接口只回状态，绝不回显凭据或密钥本身
    serialized = str(data)
    assert TEST_SESSION_SECRET not in serialized
    assert "test-api-key" not in serialized


def test_status_lists_missing_configuration_names_only(client, use_settings):
    use_settings(llm_api_key=None)
    data = client.get("/api/auth/status").json()
    assert data["llm"] == "not_configured"
    assert data["llm_missing"] == ["LLM_API_KEY"]


def test_status_warns_when_session_secret_is_missing(client, monkeypatch, test_settings):
    """签名密钥未配置时把「重启即全员掉线」写进状态，而不是只留一行启动日志。

    与下一条同样只替换派生属性：`auth_session_secret_configured` 是密钥是否配置的
    唯一判据，把它掰成 False 就能覆盖这条告警分支，而不必真的换掉签名密钥。
    """
    from app.config import Settings

    monkeypatch.setattr(Settings, "auth_session_secret_configured", property(lambda self: False))
    monkeypatch.setattr(
        Settings,
        "auth_warnings",
        property(lambda self: ["AUTH_SESSION_SECRET 未配置"]),
    )
    data = client.get("/api/auth/status").json()
    assert data["auth_session_secret_configured"] is False
    assert any("AUTH_SESSION_SECRET" in item for item in data["auth_warnings"])


def test_status_warns_when_session_secret_is_short(client, monkeypatch):
    """密钥可配置但过短时只告警、不阻断：服务照常可用，隐患写在状态里。

    只替换这一个派生属性，不动签名密钥本身——换掉密钥会让手上这张票据立刻失效，
    请求会停在 401，断言就变成了在测「换了密钥会掉线」。
    """
    from app.config import Settings

    monkeypatch.setattr(
        Settings, "auth_warnings", property(lambda self: ["AUTH_SESSION_SECRET 过短"])
    )
    data = client.get("/api/auth/status").json()
    assert any("过短" in item for item in data["auth_warnings"])


# ---- CSRF 底线 ----


def test_write_request_from_foreign_origin_is_rejected(client):
    resp = client.post("/api/auth/logout", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 403


def test_write_request_with_same_origin_is_allowed(client):
    """同源写请求必须照常通过：Origin 校验不能挡住正常使用。"""
    resp = client.post("/api/auth/logout", headers={"Origin": "http://testserver"})
    assert resp.status_code == 204


def test_read_request_is_not_origin_checked(client):
    """读请求不做同源校验：跨站读有浏览器同源策略兜着，业务上也不需要再拦一道。"""
    resp = client.get("/api/auth/session", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 200
