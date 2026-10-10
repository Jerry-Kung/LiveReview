"""复盘准则编号对照表接口的测试（V0.8.0）。

覆盖两件对外契约：谁能读（所有登录账号，含普通账号），以及读到的内容是否与提示词同源。
内容同源这条是重点——对照表存在的唯一意义就是让报告里的编号查得到，一旦它比提示词少几条、
或编号对不上，界面展示的「话术样板 04」就是错的。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.llm.mindset import MIND_SETS, SCRIPT_SAMPLES, VIOLATION_TERMS


def test_guideline_returns_full_catalogue(client: TestClient):
    """管理员读到的对照表覆盖全部心法、样板块与违规词，字段与数据结构对得上。"""
    resp = client.get("/api/guideline")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["mindsets"]) == len(MIND_SETS)
    assert len(body["samples"]) == len(SCRIPT_SAMPLES)
    assert len(body["violations"]) == len(VIOLATION_TERMS)

    # 逐条比对编号与原文：接口是数据的直出，不该在这里丢字段或改名
    assert [item["code"] for item in body["mindsets"]] == [item.code for item in MIND_SETS]
    assert [item["code"] for item in body["samples"]] == [item.code for item in SCRIPT_SAMPLES]
    for got, want in zip(body["samples"], SCRIPT_SAMPLES):
        assert got["text"] == want.text
        assert got["category"] == want.category
    for got, want in zip(body["mindsets"], MIND_SETS):
        assert got["name"] == want.name
        assert got["check"] == want.check
        assert got["sample_codes"] == list(want.sample_codes)


def test_guideline_hides_improve_guidance(client: TestClient):
    """不把准则里的「改进」原文发给客户端。

    改进动作必须由复盘结论结合本场实际给出；把通用改法也发出去，界面就有机会直接展示它，
    等于把准则禁止的空话又请了回来。
    """
    body = client.get("/api/guideline").json()
    for entry in body["mindsets"]:
        assert "improve" not in entry


def test_member_can_read_guideline(client: TestClient, member_client: TestClient):
    """普通账号读得到，且内容与管理员看到的一致。

    拿到报告的人可能就是主播或运营，他们不一定是管理员；而这个接口只有静态参考内容，
    不含任务数据与运行配置，因此不对它设管理员门槛。
    """
    assert client.get("/api/guideline").status_code == 200
    resp = member_client.get("/api/guideline")
    assert resp.status_code == 200, resp.text
    assert resp.json() == client.get("/api/guideline").json()


def test_guideline_requires_login(anonymous_client: TestClient):
    """未登录读不到：整组业务路由都挂在登录依赖下，这里确认没有被漏掉。"""
    assert anonymous_client.get("/api/guideline").status_code == 401
