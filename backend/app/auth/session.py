"""会话票据：把「谁、什么时候登录的」签名后放进 Cookie，服务端不存会话。

为什么是无状态票据而不是会话表：

- V0 单实例部署，进程重启频繁（本地开发 reload 更频繁），存内存会在每次重启后把所有人踢下线。
- 建表就要考虑过期清理与并发写，而本阶段唯一要记住的信息就是「账号 + 签发时间」，签名足够。
- 换密码或停用账号需要「立即失效所有旧票据」时，改 `AUTH_SESSION_SECRET` 即可——票据签名一变，
  旧票全部作废。这把「踢人」的能力留了下来，代价是全员重登，符合当前阶段。

票据结构：`base64url(账号).签发时间.HMAC-SHA256(账号.签发时间)`

签名覆盖账号与签发时间两者，因此改任何一个字段都会导致验签失败。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time

logger = logging.getLogger(__name__)

COOKIE_NAME = "liverreview_session"
# 签名密钥长度：低于这个长度说明用户随手填了一个短串，签名强度不足，启动时告警
MIN_SECRET_BYTES = 16


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest)


def issue(secret: str, username: str, *, issued_at: int | None = None) -> str:
    """签发一张票据。签发时间取整秒，与本机时钟无关的绝对时间戳。"""
    stamp = int(time.time()) if issued_at is None else int(issued_at)
    payload = f"{_b64encode(username.encode('utf-8'))}.{stamp}"
    return f"{payload}.{_sign(secret, payload)}"


def verify(secret: str, token: str | None, *, max_age_seconds: int) -> str | None:
    """校验票据并返回账号；无效、被篡改或已过期一律返回 None。

    过期判断用票据里自己的签发时间，因此不依赖服务端保存任何东西；时钟被回拨不会让
    旧票据复活（签名仍然成立，但 max_age 是从签发时间到当前时间的差值）。
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    raw_username, raw_stamp, signature = parts
    payload = f"{raw_username}.{raw_stamp}"
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return None
    try:
        issued_at = int(raw_stamp)
        username = _b64decode(raw_username).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not username:
        return None
    age = int(time.time()) - issued_at
    # 允许少量时钟偏移：签发时间可能因并发略大于当前秒
    if age > max_age_seconds or age < -60:
        return None
    return username


def generate_secret() -> str:
    """生成一把可用的签名密钥，供未配置时兜底与命令行使用。"""
    return secrets.token_urlsafe(32)


def secret_is_weak(secret: str) -> bool:
    """密钥过短时认为强度不足——只用于启动告警，不阻止服务运行。"""
    return len(secret.encode("utf-8")) < MIN_SECRET_BYTES
