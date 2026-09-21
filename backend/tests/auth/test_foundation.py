"""鉴权地基的单测：口令哈希、账号存取、会话签发与撤销、登录业务。"""

from __future__ import annotations

import pytest

from app.auth import session as token_module
from app.auth import store
from app.auth.password import hash_password, verify_password
from app.auth.service import LoginThrottle, authenticate

# 迭代次数压到最小：单测里跑 24 万次 PBKDF2 会让每条用例慢上几十毫秒且毫无信息量，
# 生产取值由 DEFAULT_ITERATIONS 决定，另有测试钉住它
FAST_ITERATIONS = 1_000

# 测试用的会话时长：窗口取短值才能在测试里观察到滑动续期
TTL_SECONDS = 3600
ABSOLUTE_TTL_SECONDS = 7 * 24 * 3600


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


@pytest.mark.original
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
    """格式坏掉返回 False 而不是抛异常：登录路径不该因格式问题变成 500。"""
    assert verify_password("whatever", broken) is False


# ---- 令牌摘要 ----


def test_token_digest_is_stable_and_hides_the_token():
    token = token_module.new_token()
    digest = token_module.hash_token(token)
    # 确定性：查会话是按摘要做的主键查询，摘要必须每次都一样
    assert digest == token_module.hash_token(token)
    assert len(digest) == token_module.TOKEN_DIGEST_LENGTH
    # 库里存的是摘要，明文令牌不该出现在摘要里
    assert token not in digest


def test_tokens_are_random():
    tokens = {token_module.new_token() for _ in range(50)}
    assert len(tokens) == 50


# ---- 账号 ----


def test_create_and_fetch_user(db_session_factory):
    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice", display_name="")
        # 未给显示名时取账号本身：界面上不该出现一个空称呼
        assert user.display_name == "alice"
        assert user.role == "admin"
        assert store.count_users(db) == 1
        assert store.get_user_by_username(db, "alice").id == user.id
        # 明文口令不落库
        assert "pwd-alice" not in user.password_hash
    finally:
        db.close()


def test_password_is_verifiable_and_changeable(db_session_factory):
    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        assert store.verify_user_password(user, "pwd-alice")
        store.set_password(db, user, "pwd-alice-2")
        assert store.verify_user_password(user, "pwd-alice-2")
        assert not store.verify_user_password(user, "pwd-alice")
    finally:
        db.close()


def test_duplicate_username_is_rejected(db_session_factory):
    """账号重复由唯一约束兜底：并发建号时「先查再写」本来就挡不住。"""
    from sqlalchemy.exc import IntegrityError

    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="pwd-alice")
        with pytest.raises(IntegrityError):
            store.create_user(db, username="alice", password="another")
    finally:
        db.close()


def test_delete_user_removes_its_sessions(db_session_factory):
    """删账号要连带清掉会话，否则会留下一张指向空账号的活会话。"""
    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        issued = store.create_session(db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS)
        assert store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS) is not None

        store.delete_user(db, user)
        assert store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS) is None
    finally:
        db.close()


# ---- 会话 ----


def test_session_roundtrip(db_session_factory):
    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice", display_name="爱丽丝")
        issued = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        resolved = store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS)
        assert resolved is not None
        assert resolved.user.id == user.id
        assert resolved.user.display_name == "爱丽丝"
        # 登录成功要记下时间：运维靠它判断账号还在不在用
        assert user.last_login_at is not None
    finally:
        db.close()


def test_session_token_is_not_stored_in_plaintext(db_session_factory):
    """库里只该有摘要：拿到数据库文件也换不出一个可用的会话。"""
    from app.models import UserSession

    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        issued = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        rows = list(db.query(UserSession).all())
        assert len(rows) == 1
        assert rows[0].id == token_module.hash_token(issued.token)
        assert issued.token != rows[0].id
    finally:
        db.close()


@pytest.mark.parametrize("broken", [None, "", "not-a-real-token", "x" * 64])
def test_session_rejects_unknown_token(db_session_factory, broken):
    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="pwd-alice")
        assert store.resolve_session(db, broken, ttl_seconds=TTL_SECONDS) is None
    finally:
        db.close()


def test_session_rejects_expired_and_deletes_it(db_session_factory):
    """过期会话即时删除：留着它只会让清理任务多扫一行。"""
    from datetime import timedelta

    from app.models import UserSession, utcnow

    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        issued = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        row = db.get(UserSession, token_module.hash_token(issued.token))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        assert store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS) is None
        assert db.get(UserSession, token_module.hash_token(issued.token)) is None
    finally:
        db.close()


def test_session_honours_absolute_expiry(db_session_factory):
    """滑动窗口再续也不能越过绝对上限，否则一次登录等于永久有效。"""
    from datetime import timedelta

    from app.models import UserSession, utcnow

    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        issued = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        row = db.get(UserSession, token_module.hash_token(issued.token))
        # 窗口还有效，但绝对上限已过
        row.absolute_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        assert store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS) is None
    finally:
        db.close()


def test_session_renewal_extends_the_window(db_session_factory):
    """剩余不足半个窗口时顺延：日常在用的用户不该被中途踢下线。"""
    from datetime import timedelta

    from app.models import UserSession, utcnow

    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        issued = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        row = db.get(UserSession, token_module.hash_token(issued.token))
        # 把剩余时间压到窗口的一成，落在续期阈值之内
        row.expires_at = utcnow() + timedelta(seconds=TTL_SECONDS / 10)
        db.commit()

        resolved = store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS)
        assert resolved is not None
        # 顺延后的到期时间应明显晚于顺延前
        assert resolved.expires_at > utcnow() + timedelta(seconds=TTL_SECONDS / 2)
    finally:
        db.close()


def test_session_renewal_is_throttled(db_session_factory):
    """剩余时间充裕时不写库：否则一次页面加载会变成几十次 UPDATE。"""
    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        issued = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        first = store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS)
        second = store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS)
        assert first is not None and second is not None
        # 刚签发的会话剩余时间远大于半个窗口，连续两次查证不该改动到期时间
        assert first.expires_at == second.expires_at
    finally:
        db.close()


def test_delete_session_revokes_immediately(db_session_factory):
    """这是有状态会话相对签名票据的核心增益：撤销后旧令牌再换不到任何东西。"""
    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        issued = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        assert store.delete_session(db, issued.token) is True
        assert store.resolve_session(db, issued.token, ttl_seconds=TTL_SECONDS) is None
        # 重复撤销不报错：多标签页各自点退出是正常操作
        assert store.delete_session(db, issued.token) is False
    finally:
        db.close()


def test_delete_user_sessions_can_keep_current_one(db_session_factory):
    """改口令时清掉其他设备的会话、保留当前这台，靠的就是 `keep`。"""
    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        first = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        second = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        keep_id = token_module.hash_token(second.token)

        removed = store.delete_user_sessions(db, user, keep=keep_id)
        assert removed == 1
        assert store.resolve_session(db, first.token, ttl_seconds=TTL_SECONDS) is None
        assert store.resolve_session(db, second.token, ttl_seconds=TTL_SECONDS) is not None
    finally:
        db.close()


def test_purge_expired_sessions_leaves_live_ones(db_session_factory):
    from datetime import timedelta

    from app.models import UserSession, utcnow

    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        live = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        dead = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        row = db.get(UserSession, token_module.hash_token(dead.token))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        assert store.purge_expired_sessions(db) == 1
        assert store.resolve_session(db, live.token, ttl_seconds=TTL_SECONDS) is not None
    finally:
        db.close()


def test_list_user_sessions_excludes_expired(db_session_factory):
    from datetime import timedelta

    from app.models import UserSession, utcnow

    db = db_session_factory()
    try:
        user = store.create_user(db, username="alice", password="pwd-alice")
        store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        dead = store.create_session(
            db, user, ttl_seconds=TTL_SECONDS, absolute_ttl_seconds=ABSOLUTE_TTL_SECONDS
        )
        row = db.get(UserSession, token_module.hash_token(dead.token))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        assert len(store.list_user_sessions(db, user)) == 1
    finally:
        db.close()


# ---- 登录业务 ----


def test_authenticate_success_and_failure(db_session_factory):
    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="right-pwd")
        assert authenticate(db, "alice", "right-pwd", throttle=LoginThrottle()).ok
        assert not authenticate(db, "alice", "wrong-pwd", throttle=LoginThrottle()).ok
        assert not authenticate(db, "nobody", "right-pwd", throttle=LoginThrottle()).ok
    finally:
        db.close()


def test_authenticate_isolates_failure_reasons(db_session_factory):
    """失败原因只走日志：对外只有「不通过」，不能据此判断账号是否存在。"""
    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="right-pwd")
        throttle = LoginThrottle()
        assert authenticate(db, "alice", "wrong", throttle=throttle).reason == "bad_password"
        assert authenticate(db, "nobody", "wrong", throttle=throttle).reason == "unknown_user"
    finally:
        db.close()


def test_authenticate_returns_detached_snapshot(db_session_factory):
    """交回的是普通数据对象而非 ORM 实例：调用方会先关会话再写 Cookie。"""
    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="right-pwd", display_name="爱丽丝")
        result = authenticate(db, "alice", "right-pwd", throttle=LoginThrottle())
        assert result.user is not None
        assert result.user.username == "alice"
        assert result.user.display_name == "爱丽丝"
        assert not hasattr(result.user, "_sa_instance_state")
    finally:
        db.close()


def test_throttle_blocks_after_repeated_failures(db_session_factory):
    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="right-pwd")
        throttle = LoginThrottle(window_seconds=60, limit=3)
        for _ in range(3):
            assert not authenticate(db, "alice", "wrong", throttle=throttle).ok
        result = authenticate(db, "alice", "right-pwd", throttle=throttle)
        assert result.reason == "throttled"
    finally:
        db.close()


def test_throttle_window_slides(db_session_factory):
    """窗口滑过即解锁：锁定必须是临时的，否则一次误触会长期锁死账号。"""
    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="right-pwd")
        throttle = LoginThrottle(window_seconds=60, limit=2)
        now = 1_000.0
        throttle.record_failure("alice", now=now)
        throttle.record_failure("alice", now=now)
        assert throttle.blocked("alice", now=now)
        assert not throttle.blocked("alice", now=now + 61)
    finally:
        db.close()


def test_throttle_reset_after_success(db_session_factory):
    db = db_session_factory()
    try:
        store.create_user(db, username="alice", password="right-pwd")
        throttle = LoginThrottle(window_seconds=60, limit=3)
        for _ in range(2):
            authenticate(db, "alice", "wrong", throttle=throttle)
        assert authenticate(db, "alice", "right-pwd", throttle=throttle).ok
        # 成功一次即清零：否则上午的几次失误会累积到下午把正常登录挡在门外
        for _ in range(2):
            authenticate(db, "alice", "wrong", throttle=throttle)
        assert not throttle.blocked("alice")
    finally:
        db.close()
