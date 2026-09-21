"""鉴权接口的 HTTP 契约：登录、退出、当前身份、状态，以及 Cookie 的收发与 CSRF。"""

from __future__ import annotations

from app.auth import session as token_module
from tests.conftest import TEST_DISPLAY_NAME, TEST_PASSWORD, TEST_USERNAME


def _login(client, username=TEST_USERNAME, password=TEST_PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---- 登录 ----


def test_login_sets_session_cookie(anonymous_client):
    resp = _login(anonymous_client)
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == TEST_USERNAME
    assert resp.json()["user"]["display_name"] == TEST_DISPLAY_NAME

    cookie = resp.headers["set-cookie"]
    assert token_module.COOKIE_NAME in cookie
    # 脚本读不到会话令牌；跨站请求默认不携带
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    # 本地开发是 http，标了 Secure 会让登录后立刻掉线
    assert "Secure" not in cookie


def test_login_stores_a_session_row(anonymous_client, db_session_factory):
    """登录必须在库里留下一条会话：V0.5.1 的登录态靠它判定，而不是靠 Cookie 本身。"""
    from app.auth import store

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        before = len(store.list_user_sessions(db, user))
    finally:
        db.close()

    _login(anonymous_client)

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        assert len(store.list_user_sessions(db, user)) == before + 1
    finally:
        db.close()


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


def test_login_reports_when_no_account_exists(anonymous_client, db_session_factory):
    """一个账号都没有时报 503 并给出下一步做什么。

    这不再是「配置写错」（环境变量那版的说法），而是「部署还没做完」：账号在库里，
    空库是冷启动的正常状态，文案必须指向初始化脚本，否则使用者只会反复怀疑口令。
    """
    from app.auth import store
    from app.models import User

    db = db_session_factory()
    try:
        db.query(User).delete()
        db.commit()
        assert store.count_users(db) == 0
    finally:
        db.close()

    resp = _login(anonymous_client)
    assert resp.status_code == 503
    assert "init_admin" in resp.json()["detail"]


def test_session_is_independent_of_the_browser_cookie_jar(client):
    """登录态由库里那一行决定，Cookie 只是搬运工。

    这条钉的是与签名票据那版的行为差异：那时令牌自带全部信息，进程重启（密钥随机）
    或账号从 AUTH_USERS 里摘掉都会让手上这张票失效；现在唯一的判据是库里那一行还在不在，
    因此把同一张令牌重新装回请求里就该照样有效。
    """
    token = client.cookies.get(token_module.COOKIE_NAME)
    assert token

    client.cookies.clear()
    client.cookies.set(token_module.COOKIE_NAME, token)
    assert client.get("/api/auth/session").status_code == 200


# ---- 当前身份 ----


def test_session_returns_current_user(client):
    resp = client.get("/api/auth/session")
    assert resp.status_code == 200
    assert resp.json()["user"] == {"username": TEST_USERNAME, "display_name": TEST_DISPLAY_NAME}


def test_session_requires_login(anonymous_client):
    # 未登录不是错误而是「还没有身份」，前端据此渲染登录页，因此这里也返回 401
    assert anonymous_client.get("/api/auth/session").status_code == 401


def test_session_rejects_forged_token(anonymous_client):
    anonymous_client.cookies.set(token_module.COOKIE_NAME, "forged-token-value")
    assert anonymous_client.get("/api/auth/session").status_code == 401


def test_session_rejects_expired_session(anonymous_client, db_session_factory):
    """过期后即便 Cookie 还在、库里那行也还在，一样拒绝。"""
    from datetime import timedelta

    from app.models import UserSession, utcnow

    _login(anonymous_client)
    token = anonymous_client.cookies.get(token_module.COOKIE_NAME)

    db = db_session_factory()
    try:
        row = db.get(UserSession, token_module.hash_token(token))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    assert anonymous_client.get("/api/auth/session").status_code == 401


def test_session_rejects_token_of_deleted_account(client, db_session_factory):
    """账号被删后旧会话必须立即失效：这正是有状态会话要换来的能力之一。"""
    from app.auth import store

    token = client.cookies.get(token_module.COOKIE_NAME)
    assert token

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        store.delete_user(db, user)
    finally:
        db.close()

    client.cookies.clear()
    client.cookies.set(token_module.COOKIE_NAME, token)
    assert client.get("/api/auth/session").status_code == 401


def test_changing_password_invalidates_other_sessions(client, db_session_factory):
    """改口令要清掉旧会话，否则「改了密码但被人拿着旧 Cookie 继续用」是说不通的。"""
    from app.auth import store

    token = client.cookies.get(token_module.COOKIE_NAME)
    assert token

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        store.set_password(db, user, "brand-new-password")
        assert store.delete_user_sessions(db, user) >= 1
    finally:
        db.close()

    # 拿着改口令前的那张令牌再来：库里已经没有它了
    client.cookies.clear()
    client.cookies.set(token_module.COOKIE_NAME, token)
    assert client.get("/api/auth/session").status_code == 401


# ---- 退出 ----


def test_logout_clears_cookie_and_blocks_reuse(client):
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert client.get("/api/auth/session").status_code == 401


def test_logout_deletes_the_server_side_session(client, db_session_factory):
    """退出的关键是服务端那一行被删掉，而不只是浏览器丢了 Cookie。

    V0.5 的签名票据做不到这一点：令牌若已被抄走，退出登录对抄走的那份毫无影响。
    """
    from app.auth import store

    token = client.cookies.get(token_module.COOKIE_NAME)
    assert client.post("/api/auth/logout").status_code == 204

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        assert store.list_user_sessions(db, user) == []
    finally:
        db.close()

    # 拿着旧令牌再来一次：库里那行没了，谁来都换不到身份
    client.cookies.clear()
    client.cookies.set(token_module.COOKIE_NAME, token)
    assert client.get("/api/auth/session").status_code == 401


def test_logout_is_idempotent(anonymous_client):
    """重复退出不该报错：用户可能开了多个标签页，各自都点了退出。"""
    assert anonymous_client.post("/api/auth/logout").status_code == 204
    assert anonymous_client.post("/api/auth/logout").status_code == 204


def test_logout_of_one_device_leaves_the_other_logged_in(client, db_session_factory):
    """两台设备各持一张会话，退出其中一台不该把另一台也踢下线。

    这是 V0.5 明确记着的短板（无状态票据只能换密钥让全员重登），本次补上。
    第二张会话直接由 store 签发，而不是再登录一次——登录会把同名的 Cookie 覆盖掉，
    「手上这张」与「另一台那张」就分不开了。
    """
    from app.auth import store

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        # 模拟另一台设备：同一个账号，另一张会话
        other = store.create_session(
            db, user, ttl_seconds=3600, absolute_ttl_seconds=7 * 24 * 3600
        )
        before = len(store.list_user_sessions(db, user))
        assert before == 2
    finally:
        db.close()

    assert client.post("/api/auth/logout").status_code == 204

    db = db_session_factory()
    try:
        user = store.get_user_by_username(db, TEST_USERNAME)
        # 只少了一张：当前这台下线了，另一台还在
        assert len(store.list_user_sessions(db, user)) == before - 1
    finally:
        db.close()

    # 另一台设备那张仍然有效：装回请求里照样能拿到身份
    client.cookies.clear()
    client.cookies.set(token_module.COOKIE_NAME, other.token)
    assert client.get("/api/auth/session").status_code == 200


# ---- 运行配置状态 ----


def test_status_requires_login(anonymous_client):
    assert anonymous_client.get("/api/auth/status").status_code == 401


def test_status_reports_configuration_without_secrets(client):
    data = client.get("/api/auth/status").json()
    assert data["version"] == "0.5.1"
    assert data["database"] == "ok"
    assert data["storage"] in ("configured", "not_configured", "unavailable")
    assert data["llm"] in ("configured", "not_configured")
    assert data["auth_users"] == 1
    assert data["auth_warnings"] == []
    # 状态接口只回状态，绝不回显凭据。口令哈希也不该出现在任何一处：
    # 账号数已经从「配置里有几条」变成「库里有几行」，顺手带出哈希是最容易犯的错
    serialized = str(data)
    assert "test-api-key" not in serialized
    assert "pbkdf2_sha256" not in serialized


def test_status_lists_missing_configuration_names_only(client, use_settings):
    use_settings(llm_api_key=None)
    data = client.get("/api/auth/status").json()
    assert data["llm"] == "not_configured"
    assert data["llm_missing"] == ["LLM_API_KEY"]


def test_status_counts_accounts_from_the_database(client, db_session_factory):
    from app.auth import store

    db = db_session_factory()
    try:
        store.create_user(db, username="second", password="another-password")
    finally:
        db.close()

    assert client.get("/api/auth/status").json()["auth_users"] == 2


def test_account_state_warns_when_no_account_exists(client, db_session_factory, test_settings):
    """空库时把「没人能登录」写进启动告警与状态接口。

    这里不通过 `/api/auth/status` 断言：当前这个会话正挂在被测账号上，把账号全删掉会先
    让会话失效，请求停在 401 上，测到的就不是告警分支了。改为直接问配置层——它同时
    供启动日志与状态接口使用，是这条告警的唯一来源。
    """
    from app.auth import store
    from app.models import User

    db = db_session_factory()
    try:
        db.query(User).delete()
        db.commit()
        assert store.count_users(db) == 0
    finally:
        db.close()

    count, problem = test_settings.auth_account_state()
    assert count == 0
    assert problem is not None
    assert "init_admin" in problem
    assert test_settings.auth_warnings == [problem]


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


def test_login_from_foreign_origin_is_rejected(anonymous_client):
    """登录也要挡跨站发起：否则攻击者能让受害者的浏览器替他建一个会话。"""
    resp = anonymous_client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403


def test_write_request_with_port_in_origin_is_allowed(client):
    """带端口的 Origin 必须放行：浏览器发来的 Origin 永远带端口。

    TestClient 默认的 Host 不带端口，这里显式带上，覆盖「端口一致即同源」这条判据。
    """
    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "http://testserver:12439", "Host": "testserver:12439"},
    )
    assert resp.status_code == 204


def test_write_request_is_allowed_when_proxy_strips_port_from_host(client):
    """经 nginx 访问时 Host 被 $host 去掉了端口，不能让正常登录变成 403。

    这是测试环境实际踩到的故障：浏览器发 `Origin: http://<主机>:12439`，nginx 转发
    `Host: <主机>`，逐字符比对 netloc 就会判成跨站，登录接口返回 403「请求来源不被信任」。
    """
    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "http://liverreview.example.com:12439", "Host": "liverreview.example.com"},
    )
    assert resp.status_code == 204


def test_write_request_is_allowed_with_forwarded_host(client):
    """外层网关改写 Host 时，按 X-Forwarded-Host 判定来源。"""
    resp = client.post(
        "/api/auth/logout",
        headers={
            "Origin": "http://liverreview.example.com:12439",
            "Host": "backend:12439",
            "X-Forwarded-Host": "liverreview.example.com:12439",
        },
    )
    assert resp.status_code == 204


def test_forwarded_host_takes_first_hop(client):
    """X-Forwarded-Host 有多个取值时按第一个（最靠近客户端的一跳）判定。"""
    resp = client.post(
        "/api/auth/logout",
        headers={
            "Origin": "http://liverreview.example.com:12439",
            "Host": "backend:12439",
            "X-Forwarded-Host": "liverreview.example.com:12439, inner-gateway",
        },
    )
    assert resp.status_code == 204


def test_foreign_origin_with_same_hostname_different_port_is_rejected(client):
    """主机名相同但端口不同仍是跨站，端口必须一致。"""
    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "http://testserver:9999", "Host": "testserver:12439"},
    )
    assert resp.status_code == 403
