"""初始化脚本的测试：建号、幂等、口令确认、非交互模式，以及建出来的号真的能登录。

脚本是冷启动阶段唯一的建号入口，它出错的表现是「服务起来了但谁也登不上」，因此这里
除了覆盖 CLI 自身的行为，还钉住「脚本建出来的账号能通过登录接口」这条端到端结论。
"""

from __future__ import annotations

import pytest

from app.auth import init_admin, store
from app.models import ROLE_ADMIN, User


@pytest.fixture
def cli_db(db_session_factory, monkeypatch):
    """把脚本用的数据库会话指向测试库。

    脚本自己开 `SessionLocal`（它没有依赖注入可言），因此要替换的是 `app.database` 上
    那个名字；同时拦掉 `init_db`，表已由夹具建好，脚本再建一次会连到本机文件库上。
    """
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)
    monkeypatch.setattr("app.database.init_db", lambda: None)
    return db_session_factory


def _feed(monkeypatch, answers: list[str]) -> None:
    """按顺序喂给 input() 的答案。用完还问就抛错，避免脚本多问了一句却没被发现。"""
    pending = list(answers)

    def fake_input(prompt: str = "") -> str:
        if not pending:
            raise AssertionError(f"脚本多问了：{prompt!r}")
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def _feed_password(monkeypatch, passwords: list[str]) -> None:
    pending = list(passwords)

    def fake_getpass(prompt: str = "") -> str:
        if not pending:
            raise AssertionError(f"脚本多要了一次口令：{prompt!r}")
        return pending.pop(0)

    monkeypatch.setattr(init_admin.getpass, "getpass", fake_getpass)


def test_non_interactive_creates_admin(cli_db, capsys):
    assert init_admin.main(["--username", "admin", "--password", "admin-password"]) == 0
    assert "创建成功" in capsys.readouterr().out

    db = cli_db()
    try:
        user = store.get_user_by_username(db, "admin")
        assert user is not None
        assert user.role == ROLE_ADMIN
        # 未给显示名时取账号本身：界面上不该出现一个空称呼
        assert user.display_name == "admin"
        assert store.verify_user_password(user, "admin-password")
    finally:
        db.close()


def test_account_created_by_cli_can_log_in(client, cli_db, db_session_factory):
    """脚本建出来的号必须能通过登录接口——这是它存在的唯一理由。"""
    assert init_admin.main(["--username", "boss", "--password", "boss-password"]) == 0

    resp = client.post("/api/auth/login", json={"username": "boss", "password": "boss-password"})
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "boss"


def test_display_name_is_kept(cli_db):
    init_admin.main(["--username", "admin", "--password", "admin-password", "--display-name", "管理员"])
    db = cli_db()
    try:
        assert store.get_user_by_username(db, "admin").display_name == "管理员"
    finally:
        db.close()


def test_interactive_flow_matches_the_documented_prompts(cli_db, monkeypatch, capsys):
    """交互式的提示与顺序是任务方验收的一部分，这里按文档里的样子钉住。"""
    prompts: list[str] = []

    def recording_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""  # 账号直接回车，取默认的 admin

    def recording_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return "admin-password"

    monkeypatch.setattr("builtins.input", recording_input)
    monkeypatch.setattr(init_admin.getpass, "getpass", recording_getpass)

    assert init_admin.main([]) == 0
    joined = "".join(prompts)
    assert "管理员用户名" in joined
    assert "管理员密码" in joined
    assert "再次输入密码" in joined
    assert "创建成功" in capsys.readouterr().out

    db = cli_db()
    try:
        assert store.get_user_by_username(db, "admin") is not None
    finally:
        db.close()


def test_interactive_requires_password_confirmation(cli_db, monkeypatch, capsys):
    """两次不一致即为输入错误：退出码非 0，且不留下任何账号。"""
    _feed(monkeypatch, [""])
    _feed_password(monkeypatch, ["first-password", "second-password"])

    assert init_admin.main([]) == 2
    assert "不一致" in capsys.readouterr().err

    db = cli_db()
    try:
        assert store.count_users(db) == 0
    finally:
        db.close()


def test_rejects_short_password(cli_db, capsys):
    """长度下限拦的是「手一滑回车了」这类输入，不是口令策略。"""
    assert init_admin.main(["--username", "admin", "--password", "short"]) == 2
    assert "过短" in capsys.readouterr().err


def test_existing_account_resets_password_only_after_confirmation(cli_db, monkeypatch, capsys):
    """账号已存在时不静默覆盖：那等于给了一条顺手改掉别人口令的路径。"""
    init_admin.main(["--username", "admin", "--password", "original-password"])

    _feed(monkeypatch, ["n"])
    assert init_admin.main(["--username", "admin", "--password", "new-password"]) == 1
    assert "已取消" in capsys.readouterr().out

    db = cli_db()
    try:
        user = store.get_user_by_username(db, "admin")
        assert store.verify_user_password(user, "original-password")
    finally:
        db.close()


def test_reset_password_yes_replaces_it_and_revokes_sessions(cli_db, monkeypatch, capsys):
    """确认重设后口令真的换掉，且旧会话一并作废。"""
    init_admin.main(["--username", "admin", "--password", "original-password"])

    db = cli_db()
    try:
        user = store.get_user_by_username(db, "admin")
        store.create_session(db, user, ttl_seconds=3600, absolute_ttl_seconds=86400)
        assert len(store.list_user_sessions(db, user)) == 1
    finally:
        db.close()

    _feed(monkeypatch, ["y"])
    assert init_admin.main(["--username", "admin", "--password", "new-password"]) == 0

    db = cli_db()
    try:
        user = store.get_user_by_username(db, "admin")
        assert store.verify_user_password(user, "new-password")
        assert not store.verify_user_password(user, "original-password")
        # 口令变了，旧登录态不该继续有效
        assert store.list_user_sessions(db, user) == []
        assert store.count_users(db) == 1
    finally:
        db.close()


def test_force_resets_without_asking(cli_db, monkeypatch):
    """`--force` 供脚本使用：不问了，也就不该再调用 input。"""
    init_admin.main(["--username", "admin", "--password", "original-password"])

    def explode(prompt: str = "") -> str:
        raise AssertionError("--force 不该再询问")

    monkeypatch.setattr("builtins.input", explode)
    assert init_admin.main(["--username", "admin", "--password", "new-password", "--force"]) == 0


def test_list_reports_accounts_without_leaking_hashes(cli_db, capsys):
    init_admin.main(["--username", "admin", "--password", "admin-password", "--display-name", "管理员"])
    capsys.readouterr()

    assert init_admin.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "admin" in out
    assert "管理员" in out
    # 明文口令与哈希都不该出现在输出里
    assert "admin-password" not in out
    assert "pbkdf2_sha256" not in out


def test_list_on_empty_database_explains_what_to_do(cli_db, capsys):
    db = cli_db()
    try:
        db.query(User).delete()
        db.commit()
    finally:
        db.close()

    assert init_admin.main(["--list"]) == 0
    assert "init_admin" in capsys.readouterr().out
