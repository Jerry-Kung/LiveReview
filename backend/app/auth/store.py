"""账号与会话的存取：本模块是 `users` / `sessions` 两张表的唯一读写点。

分成两块职责：

- **账号**：按账号查人、新建、改口令。口令哈希的生成与校验在 `app.auth.password`，
  这里只负责把它存进去、取出来。
- **会话**：签发、查证、撤销。这里有一处与 V0.5 签名票据不同的关键性质——**会话在
  服务端有记录，因此可以单独撤销**。`delete_session` 让「退出登录」变成真的失效，
  `delete_user_sessions` 让改口令或停用账号能立刻把旧会话清掉。

令牌本身的生成与摘要算法在 `app.auth.session`（那里也放着 Cookie 名），本模块只管
「把它记进表里」「按摘要查出来」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from app.auth.password import hash_password, verify_password
from app.auth.session import hash_token, new_token
from app.models import ROLE_ADMIN, User, UserSession, utcnow

logger = logging.getLogger(__name__)


def _as_utc(value: datetime | None) -> datetime | None:
    """把库里读出的时间统一成带时区的 UTC。

    SQLite 不保存时区，`DateTime(timezone=True)` 存进去的其实是一个朴素值；直接与
    `utcnow()` 相减会抛「can't subtract offset-naive and offset-aware datetimes」。
    写入时一律带时区、读出时按 UTC 补齐，两边就对齐了。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---- 账号 ----


def count_users(db: DbSession) -> int:
    """账号总数。登录接口用它判断「服务端是否还没有任何账号」。"""
    return int(db.scalar(select(func.count()).select_from(User)) or 0)


def get_user_by_username(db: DbSession, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == username))


def get_user_by_id(db: DbSession, user_id: int) -> User | None:
    return db.get(User, user_id)


def create_user(
    db: DbSession,
    *,
    username: str,
    password: str,
    display_name: str = "",
    role: str = ROLE_ADMIN,
) -> User:
    """新建账号。口令在这里就变成哈希，明文不进 ORM 对象也不进日志。

    账号重复由 `users.username` 的唯一约束兜底，调用方无须先查一次——「先查再写」
    在并发下本来也挡不住重复。
    """
    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name or username,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("已创建账号：%s", username)
    return user


def set_password(db: DbSession, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    db.commit()


def verify_user_password(user: User, password: str) -> bool:
    return verify_password(password, user.password_hash)


def count_users_by_role(db: DbSession, role: str) -> int:
    """指定角色的账号数：初始化脚本用它判断「是否已有管理员」。"""
    return int(db.scalar(select(func.count()).select_from(User).where(User.role == role)) or 0)


def list_users(db: DbSession) -> list[User]:
    """按账号排序的账号清单，供运维查看（口令哈希不出现在任何输出里）。"""
    return list(db.scalars(select(User).order_by(User.username)))


def delete_user(db: DbSession, user: User) -> None:
    """删除账号。会话由外键的级联删除一并清掉，不会留下指向空账号的活会话。"""
    db.delete(user)
    db.commit()
    logger.info("已删除账号：%s", user.username)


# ---- 会话 ----


@dataclass(frozen=True)
class IssuedSession:
    """一次签发的结果：令牌只在这一次返回，之后服务端只有摘要。"""

    token: str
    expires_at: datetime
    absolute_expires_at: datetime


def create_session(
    db: DbSession,
    user: User,
    *,
    ttl_seconds: int,
    absolute_ttl_seconds: int,
    user_agent: str | None = None,
) -> IssuedSession:
    """签发一张会话，并记下这次登录。

    `expires_at` 是滑动窗口的起点，`absolute_expires_at` 是它的上限；两者都由这里
    定下，后续 `touch_session` 只会顺延前者。
    """
    now = utcnow()
    token = new_token()
    expires_at = now + timedelta(seconds=ttl_seconds)
    absolute_expires_at = now + timedelta(seconds=absolute_ttl_seconds)
    db.add(
        UserSession(
            id=hash_token(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            absolute_expires_at=absolute_expires_at,
            # 只留前 255 个字符：这是排查线索，不值得为它放宽列宽
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    user.last_login_at = now
    db.commit()
    return IssuedSession(
        token=token, expires_at=expires_at, absolute_expires_at=absolute_expires_at
    )


@dataclass(frozen=True)
class ResolvedSession:
    """查证通过的会话：调用方需要的账号与这张会话的到期时间。"""

    user: User
    session_id: str
    expires_at: datetime
    absolute_expires_at: datetime
    last_seen_at: datetime


def resolve_session(
    db: DbSession,
    token: str | None,
    *,
    renew: bool = True,
    ttl_seconds: int = 0,
) -> ResolvedSession | None:
    """查证令牌并返回会话与账号；无效、已过期或账号已删除一律返回 None。

    **滑动续期**：剩余时间不足半个窗口时把 `expires_at` 往后推，且不超过
    `absolute_expires_at`。节流是必要的——每个 API 请求都写一次库，会让一次页面
    加载（轮询、分片上传、列表刷新）变成几十次 UPDATE，而把窗口顺延几秒并没有意义。
    """
    if not token:
        return None
    session = db.get(UserSession, hash_token(token))
    if session is None:
        return None

    now = utcnow()
    expires_at = _as_utc(session.expires_at)
    absolute_expires_at = _as_utc(session.absolute_expires_at)
    if expires_at is None or absolute_expires_at is None:
        return None
    if now >= expires_at or now >= absolute_expires_at:
        # 到期即删：留着它只是让清理任务多扫一行，而这一行已经不可能再通过校验
        db.execute(delete(UserSession).where(UserSession.id == session.id))
        db.commit()
        return None

    user = db.get(User, session.user_id)
    if user is None:
        # 正常路径下外键级联已清掉会话，这里是防御：账号没了就不该再有可用会话
        db.execute(delete(UserSession).where(UserSession.id == session.id))
        db.commit()
        return None

    if renew and ttl_seconds > 0:
        remaining = expires_at - now
        # 剩余不足半个窗口才续期：续期写入的次数因此与「会话长度 / 窗口」同阶，
        # 而不是与请求数同阶
        if remaining < timedelta(seconds=ttl_seconds / 2):
            extended = min(now + timedelta(seconds=ttl_seconds), absolute_expires_at)
            if extended > expires_at:
                session.expires_at = extended
                expires_at = extended
            session.last_seen_at = now
            db.commit()

    return ResolvedSession(
        user=user,
        session_id=session.id,
        expires_at=expires_at,
        absolute_expires_at=absolute_expires_at,
        last_seen_at=_as_utc(session.last_seen_at) or now,
    )


def delete_session(db: DbSession, token: str | None) -> bool:
    """撤销一张会话。返回是否真的删掉了一条记录。

    这是有状态会话相对签名票据的核心增益：退出登录后**服务端不再认这张令牌**，
    而不是只让浏览器把 Cookie 丢掉——旧令牌若已被抄走，仍然一文不值。
    """
    if not token:
        return False
    result = db.execute(delete(UserSession).where(UserSession.id == hash_token(token)))
    db.commit()
    return bool(result.rowcount)


def delete_user_sessions(db: DbSession, user: User, *, keep: str | None = None) -> int:
    """清掉某个账号的全部会话，返回清除条数。

    用于改口令与停用账号：口令换了，旧会话不该继续有效。`keep` 可以指定要保留的
    会话 id（改成「改完口令后当前这台设备不掉线」时用得上）。
    """
    statement = delete(UserSession).where(UserSession.user_id == user.id)
    if keep:
        statement = statement.where(UserSession.id != keep)
    result = db.execute(statement)
    db.commit()
    return int(result.rowcount or 0)


def purge_expired_sessions(db: DbSession) -> int:
    """清掉已过期的会话，返回清除条数。

    到期会话在 `resolve_session` 撞上时会即时删除，这里处理的是**再没人用过**的那些：
    用户登录一次就再也没回来，那张记录只会一直躺在表里。
    """
    now = utcnow()
    result = db.execute(delete(UserSession).where(UserSession.expires_at <= now))
    db.commit()
    return int(result.rowcount or 0)


def list_user_sessions(db: DbSession, user: User) -> list[UserSession]:
    """某个账号当前有效的会话，供「已登录设备」这类查看用到。"""
    now = utcnow()
    return list(
        db.scalars(
            select(UserSession)
            .where(UserSession.user_id == user.id, UserSession.expires_at > now)
            .order_by(UserSession.last_seen_at.desc())
        )
    )
