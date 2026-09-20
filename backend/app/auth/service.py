"""登录业务：凭据比对与失败节流。不涉及 HTTP，Cookie 的读写由路由层负责。

节流放在进程内存里：V0 单实例部署，重启即清空——这是可接受的，因为节流的目标是
挡住「对着一个已知账号不停试口令」这类在线猜测，而不是做长期风控。多实例部署时
应换成共享存储（Redis 或数据库），届时这里的接口形态不必改。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.auth.config import AuthUser
from app.auth.password import verify_password

logger = logging.getLogger(__name__)

# 节流窗口与阈值：同一账号 1 分钟内连续失败 5 次即锁定，直到窗口滑过。
# 阈值取小是因为本阶段用户数极少、登录频率极低，正常用户几乎不可能触发。
FAILURE_WINDOW_SECONDS = 60
FAILURE_LIMIT = 5


@dataclass(frozen=True)
class LoginResult:
    """登录结果。`reason` 只用于日志，不返回给客户端（对外一律「账号或密码不正确」）。"""

    user: AuthUser | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.user is not None


class LoginThrottle:
    """按账号记录失败次数，纯内存实现。"""

    def __init__(self, window_seconds: int = FAILURE_WINDOW_SECONDS, limit: int = FAILURE_LIMIT):
        self._window = window_seconds
        self._limit = limit
        self._failures: dict[str, list[float]] = {}

    def _recent(self, username: str, now: float) -> list[float]:
        stamps = [stamp for stamp in self._failures.get(username, []) if now - stamp < self._window]
        if stamps:
            self._failures[username] = stamps
        else:
            self._failures.pop(username, None)
        return stamps

    def blocked(self, username: str, *, now: float | None = None) -> bool:
        """该账号是否已被锁定。"""
        moment = time.monotonic() if now is None else now
        return len(self._recent(username, moment)) >= self._limit

    def record_failure(self, username: str, *, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        stamps = self._recent(username, moment)
        stamps.append(moment)
        self._failures[username] = stamps

    def clear(self, username: str) -> None:
        self._failures.pop(username, None)


# 进程内单例：与配置、存储实例同样通过函数获取，避免全局可变状态散落
_throttle = LoginThrottle()


def get_throttle() -> LoginThrottle:
    return _throttle


def authenticate(
    users: dict[str, AuthUser],
    username: str,
    password: str,
    *,
    throttle: LoginThrottle | None = None,
) -> LoginResult:
    """比对凭据。返回的失败原因只写日志，对外文案由路由层统一给。"""
    limiter = throttle if throttle is not None else _throttle
    if limiter.blocked(username):
        logger.warning("账号 %s 登录失败次数过多，已在节流窗口内拒绝", username)
        return LoginResult(user=None, reason="throttled")

    user = users.get(username)
    if user is None:
        limiter.record_failure(username)
        return LoginResult(user=None, reason="unknown_user")
    if not verify_password(password, user.password_hash):
        limiter.record_failure(username)
        return LoginResult(user=None, reason="bad_password")

    limiter.clear(username)
    return LoginResult(user=user, reason="ok")
