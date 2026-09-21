"""路由收口：所有数据接口在未登录时都必须拒绝，登录后照常可用。

逐条列举而不是抽一条代表：路由装配处漏挂一个依赖，只会在这条路由上留下一个洞，
而「随便挑一个路由测一下」正好测不出漏掉的那个。这里枚举的是全部业务端点，
新增端点时这张清单也要一起长。
"""

from __future__ import annotations

import pytest

# 业务端点的读写方法与路径。路径里的 id 用不存在的占位值：未登录时应当先被鉴权拦住，
# 不会走到业务分支，因此不需要真实数据。
PROTECTED_ENDPOINTS: list[tuple[str, str]] = [
    ("POST", "/api/uploads"),
    ("PUT", "/api/uploads/unknown-id/chunks/0"),
    ("GET", "/api/uploads/unknown-id"),
    ("POST", "/api/uploads/unknown-id/complete"),
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/unknown-id"),
    ("POST", "/api/tasks/unknown-id/retry"),
    ("DELETE", "/api/tasks/unknown-id"),
    ("POST", "/api/tasks/unknown-id/understanding"),
    ("POST", "/api/tasks/unknown-id/clips/0/understanding"),
    ("GET", "/api/tasks/unknown-id/transcript"),
    ("GET", "/api/tasks/unknown-id/transcript.txt"),
    ("POST", "/api/tasks/unknown-id/review"),
    ("GET", "/api/tasks/unknown-id/review.md"),
    # 运行配置状态原先挂在公开的健康检查上，V0.5 起移到这里，同样要求登录
    ("GET", "/api/auth/status"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
def test_endpoint_rejects_anonymous(anonymous_client, method, path):
    resp = anonymous_client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} 未登录却被放行（HTTP {resp.status_code}）"


@pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
def test_endpoint_rejects_forged_token(anonymous_client, method, path):
    """伪造的令牌同样不许通行：必须真的到库里查过，而不是只看 Cookie 存在。"""
    from app.auth import session as token_module

    anonymous_client.cookies.set(token_module.COOKIE_NAME, "forged-token-value")
    resp = anonymous_client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} 接受了一张伪造令牌"


@pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
def test_endpoint_reaches_business_logic_when_logged_in(anonymous_client, method, path):
    """登录后不再被鉴权拦住。

    这里不要求业务成功：占位 id 会得到 404、缺请求体会得到 422，都属于「已进入业务分支」。
    真正要钉住的是**不再出现 401**——那才是鉴权放行的判据。
    """
    from tests.conftest import login

    login(anonymous_client)
    resp = anonymous_client.request(method, path)
    assert resp.status_code != 401, f"{method} {path} 登录后仍被判为未登录"


def test_health_stays_public(anonymous_client):
    """健康检查必须保持公开：编排探针不会登录，拦下它等于让容器永远起不来。"""
    resp = anonymous_client.get("/health")
    assert resp.status_code == 200


def test_login_endpoint_stays_public(anonymous_client):
    """登录接口本身不能要求登录，否则没人能拿到第一张票据。"""
    from tests.conftest import TEST_PASSWORD, TEST_USERNAME

    resp = anonymous_client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200


def test_transcript_download_requires_login(client):
    """下载口由浏览器直接发起请求，不走前端封装，因此要在服务端同样把住。

    用真实任务而不是占位 id：占位 id 在未登录时被鉴权拦住、登录后变成 404，
    断言就分不清「被鉴权拦住」与「任务不存在」，也就测不出这里真正要测的东西。
    """
    from app.auth import session as ticket
    from tests.conftest import login

    created = client.post("/api/uploads", json={"filename": "live.mp4", "size": 1024})
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    assert client.get(f"/api/tasks/{task_id}/transcript.txt").status_code == 200

    client.cookies.delete(ticket.COOKIE_NAME)
    assert client.get(f"/api/tasks/{task_id}/transcript.txt").status_code == 401

    login(client)
    assert client.get(f"/api/tasks/{task_id}/transcript.txt").status_code == 200


def test_review_download_requires_login(client, model):
    """复盘报告下载口同上：报告里有整场直播的结论，比转写正文更不该对未登录者开放。

    这里把整条链路跑通（上传 → 切分 → 识别 → 复盘），使 401 与 200 的差别只来自会话，
    而不是「还没有可下载的结论」这种业务状态。
    """
    from app.auth import session as ticket
    from tests.conftest import login
    from tests.test_review import _prepare, _understood

    task_id = _prepare(client)
    _understood(client, task_id)
    assert client.post(f"/api/tasks/{task_id}/review").status_code == 202
    assert client.get(f"/api/tasks/{task_id}/review.md").status_code == 200

    client.cookies.delete(ticket.COOKIE_NAME)
    assert client.get(f"/api/tasks/{task_id}/review.md").status_code == 401

    login(client)
    assert client.get(f"/api/tasks/{task_id}/review.md").status_code == 200
