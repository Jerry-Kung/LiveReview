"""鉴权地基的单测：口令哈希、用户配置解析、票据签名与登录业务。"""

from __future__ import annotations

import time

import pytest

from app.auth import session as ticket
from app.auth.config import AuthConfigError, parse_users
from app.auth.password import hash_password, verify_password
from app.auth.service import LoginThrottle, authenticate

# 迭代次数压到最小：单测里跑 24 万次 PBKDF2 会让每条用例慢上几十毫秒且毫无信息量，
# 生产取值由 DEFAULT_ITERATIONS 决定，另有测试钉住它
FAST_ITERATIONS = 1_000


def _users(*pairs: tuple[str, str]) -> str:
    return ",".join(f"{name}:{hash_password(pwd, iterations=FAST_ITERATIONS)}" for name, pwd in pairs)


# ---- 口令哈希 ----


def test_password_roundtrip():
    encoded = hash_password("Correct-Horse-1", iterations=FAST_ITERATIONS)
    assert verify_password("Correct-Horse-1", encoded)
    assert not verify_password("correct-horse-1", encoded)
    assert not verify_password("", encoded)


def test_password_hash_has_no_plaintext_and_is_salted():
    first = hash_password("same-password", iterations=FAST_ITERATIONS)
    second = hash_password("same-password", iterations=FAST_ITERATIONS)
    assert "same-password" not in first
    # 同一口令两次哈希必须不同：盐相同就等于给了彩虹表一张通行证
    assert first != second
    assert first.split("$")[0] == "pbkdf2_sha256"


def test_default_iterations_meet_current_guidance():
    """默认迭代次数不低于 20 万：数值写小了不会有任何测试失败，只会静默变弱。"""
    from app.auth.password import DEFAULT_ITERATIONS

    assert DEFAULT_ITERATIONS >= 200_000


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "plain-text",
        "pbkdf2_sha256$abc$00$00",
        "md5$1000$00$00",
        "pbkdf2_sha256$1000$zz$00",
        "pbkdf2_sha256$1000$00",
    ],
)
def test_verify_rejects_malformed_hash(broken):
    """格式坏掉返回 False 而不是抛异常：登录路径不该因配置问题变成 500。"""
    assert verify_password("whatever", broken) is False


# ---- 用户配置解析 ----


def test_parse_users_with_display_name():
    raw = _users(("alice", "pwd-alice"))
    users = parse_users(raw)
    assert list(users) == ["alice"]
    assert users["alice"].display_name == "alice"  # 未写显示名时取账号本身


def test_parse_users_three_fields():
    encoded = hash_password("pwd", iterations=FAST_ITERATIONS)
    users = parse_users(f"alice:{encoded}:爱丽丝")
    assert users["alice"].display_name == "爱丽丝"


def test_parse_users_multiple_and_blank_entries():
    raw = _users(("alice", "a"), ("bob", "b"))
    users = parse_users(f" {raw} , ")
    assert set(users) == {"alice", "bob"}


def test_parse_users_empty_returns_empty_map():
    assert parse_users(None) == {}
    assert parse_users("   ") == {}


@pytest.mark.parametrize(
    "raw",
    [
        "alice",  # 缺口令哈希
        "alice:",  # 口令哈希为空
        ":hash",  # 账号为空
        "alice:hash:显示:多余",  # 段数过多
        "ali,ce:hash" ,  # 账号含分隔符
    ],
)
def test_parse_users_rejects_broken_config(raw):
    """配置写坏必须启动即失败：静默跳过的后果是「某账号怎么也登不上」却没有任何提示。"""
    with pytest.raises(AuthConfigError):
        parse_users(raw)


def test_parse_users_rejects_duplicate_account():
    encoded = hash_password("pwd", iterations=FAST_ITERATIONS)
    with pytest.raises(AuthConfigError):
        parse_users(f"alice:{encoded},alice:{encoded}")


# ---- 会话票据 ----


def test_ticket_roundtrip():
    token = ticket.issue("secret-key-for-test", "alice")
    assert ticket.verify("secret-key-for-test", token, max_age_seconds=60) == "alice"


def test_ticket_rejects_tampered_username():
    token = ticket.issue("secret-key-for-test", "alice")
    parts = token.split(".")
    forged = f"{ticket._b64encode(b'bob')}.{parts[1]}.{parts[2]}"
    assert ticket.verify("secret-key-for-test", forged, max_age_seconds=60) is None


def test_ticket_rejects_other_secret():
    """换密钥即作废全部旧票据：这是本版「踢人」能力的实现方式。"""
    token = ticket.issue("first-secret-value", "alice")
    assert ticket.verify("second-secret-value", token, max_age_seconds=60) is None


def test_ticket_rejects_expired():
    token = ticket.issue("secret-key-for-test", "alice", issued_at=int(time.time()) - 7200)
    assert ticket.verify("secret-key-for-test", token, max_age_seconds=3600) is None


@pytest.mark.parametrize("broken", [None, "", "abc", "a.b", "a.b.c.d", "not.a.ticket"])
def test_ticket_rejects_malformed(broken):
    assert ticket.verify("secret-key-for-test", broken, max_age_seconds=60) is None


def test_fallback_secret_is_stable_within_process(monkeypatch):
    """未配置密钥时同一进程内必须稳定：签发与校验用两把不同密钥会让登录永远失败。"""
    from app.config import Settings

    settings = Settings(_env_file=None, auth_session_secret=None)
    assert settings.auth_session_secret_value == settings.auth_session_secret_value
    assert len(settings.auth_session_secret_value) > 16


def test_configured_secret_wins():
    from app.config import Settings

    settings = Settings(_env_file=None, auth_session_secret="explicit-secret-value")
    assert settings.auth_session_secret_value == "explicit-secret-value"
    assert settings.auth_session_secret_configured is True


# ---- 登录业务 ----


def test_authenticate_success_and_failure():
    users = parse_users(_users(("alice", "right-pwd")))
    assert authenticate(users, "alice", "right-pwd", throttle=LoginThrottle()).ok
    assert not authenticate(users, "alice", "wrong-pwd", throttle=LoginThrottle()).ok
    assert not authenticate(users, "nobody", "right-pwd", throttle=LoginThrottle()).ok


def test_authenticate_isolates_failure_reasons():
    """失败原因只走日志：对外只有「不通过」，不能据此判断账号是否存在。"""
    users = parse_users(_users(("alice", "right-pwd")))
    throttle = LoginThrottle()
    assert authenticate(users, "alice", "wrong", throttle=throttle).reason == "bad_password"
    assert authenticate(users, "nobody", "wrong", throttle=throttle).reason == "unknown_user"


def test_throttle_blocks_after_repeated_failures():
    users = parse_users(_users(("alice", "right-pwd")))
    throttle = LoginThrottle(window_seconds=60, limit=3)
    for _ in range(3):
        assert not authenticate(users, "alice", "wrong", throttle=throttle).ok
    result = authenticate(users, "alice", "right-pwd", throttle=throttle)
    assert result.reason == "throttled"


def test_throttle_window_slides():
    """窗口滑过即解锁：锁定必须是临时的，否则一次误触会长期锁死账号。"""
    users = parse_users(_users(("alice", "right-pwd")))
    throttle = LoginThrottle(window_seconds=60, limit=2)
    now = 1_000.0
    throttle.record_failure("alice", now=now)
    throttle.record_failure("alice", now=now)
    assert throttle.blocked("alice", now=now)
    assert not throttle.blocked("alice", now=now + 61)


def test_throttle_reset_after_success():
    users = parse_users(_users(("alice", "right-pwd")))
    throttle = LoginThrottle(window_seconds=60, limit=3)
    for _ in range(2):
        authenticate(users, "alice", "wrong", throttle=throttle)
    assert authenticate(users, "alice", "right-pwd", throttle=throttle).ok
    # 成功一次即清零：否则上午的几次失误会累积到下午把正常登录挡在门外
    for _ in range(2):
        authenticate(users, "alice", "wrong", throttle=throttle)
    assert not throttle.blocked("alice")
