"""鉴权对外入口：FastAPI 依赖、Cookie 读写与 CSRF 底线。

这一层是「谁能访问」的唯一判定点，业务路由只依赖 `require_user`，不自己解析 Cookie。

两道防线：

1. **登录态**：`require_user` 从 Cookie 取令牌，到 `sessions` 表查证并检查有效期，
   未通过一律 401。令牌无效、会话过期、账号已删除走同一条拒绝路径。
2. **CSRF**：写操作（非 GET/HEAD/OPTIONS）校验 `Origin` 是否与请求自身的 Host 同源。
   Cookie 是浏览器自动携带的凭证，`SameSite=Lax` 已能挡住跨站的表单与 fetch，
   但同站子域或放宽策略时仍会被利用，因此对写操作再加一条同源检查。
   没有 `Origin` 的请求放行——那是非浏览器客户端（curl、脚本）发出的，它们本就不带 Cookie。

会话查证每次请求都要读一次库（V0.5 的签名票据不需要），这是把会话收进数据库的代价，
换来的是「能单独撤销一张会话」。SQLite 上的主键查询在这个量级不构成瓶颈。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status

from app.auth import session as session_cookie
from app.auth import store

logger = logging.getLogger(__name__)

# 这些方法不改变服务端状态，不做同源校验
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _settings():
    """取应用配置。

    在函数内导入而不是在模块顶层：`app.config` 要读本包来派生鉴权字段（会话有效期、
    启动告警），顶层互相导入会形成环——谁先被导入谁就拿到半成品模块，表现为
    `cannot import name 'Settings' from partially initialized module`。

    换成构造期注入（`Depends(get_settings)`）也解决不了：依赖注入要求配置对象本身
    已经能构造，而构造它正需要本包，环还在。
    """
    from app.config import get_settings

    return get_settings()


def _db_session():
    """打开一个数据库会话，供鉴权依赖自行使用。

    鉴权依赖不通过 `Depends(get_db)` 注入：它挂在整组业务路由上，而各路由自己也要
    拿 `get_db`，两者共用同一个会话对象会让「鉴权阶段提交了事务」与「业务阶段的事务」
    纠缠在一起。这里各开一个连接，职责清楚，代价是一次连接开销。
    """
    from app.database import SessionLocal

    return SessionLocal()


@dataclass(frozen=True)
class CurrentUser:
    """当前登录者。界面用它显示称呼；本版不做用户隔离，因此不参与数据查询条件。"""

    user_id: int
    username: str
    display_name: str
    role: str


def _is_same_origin(request: Request) -> bool:
    """请求的 Origin 是否与本请求的 Host 同源。

    Origin 缺失即放行（见模块说明）。比对时忽略端口以外的差异，协议与主域都必须一致：
    `http` 与 `https` 视为不同源，避免在降级场景下被绕过。
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    host = request.headers.get("host")
    if not host or not parsed.netloc:
        return False
    return parsed.netloc.lower() == host.lower()


def origin_allowed(request: Request) -> bool:
    """写操作的来源是否可信。读操作与非浏览器客户端一律放行（见模块说明）。"""
    if request.method in SAFE_METHODS:
        return True
    return _is_same_origin(request)


async def require_trusted_origin(request: Request) -> None:
    """只做同源校验、不要求登录的依赖。

    给「退出登录」这类端点用：它不需要登录态（开多个标签页各自点退出不该报错），
    但确实改变客户端状态，同源校验仍要保留。
    """
    if not origin_allowed(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请求来源不被信任")


def _unauthorized() -> HTTPException:
    """统一的拒绝响应。会话不存在、过期、账号被删都走这一条。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="登录状态已失效，请重新登录",
    )


def read_session_token(request: Request) -> str | None:
    return request.cookies.get(session_cookie.COOKIE_NAME)


def require_user(request: Request) -> CurrentUser:
    """路由依赖：未登录、令牌无效、会话过期或来源不可信时拒绝。"""
    if not origin_allowed(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请求来源不被信任")

    settings = _settings()
    token = read_session_token(request)
    db = _db_session()
    try:
        resolved = store.resolve_session(
            db,
            token,
            ttl_seconds=settings.auth_session_ttl_seconds,
        )
        if resolved is None:
            raise _unauthorized()
        user = resolved.user
        return CurrentUser(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
        )
    finally:
        db.close()


def set_session_cookie(response: Response, settings, token: str) -> None:
    """把令牌写进 Cookie。

    - `httponly`：脚本读不到，XSS 也偷不走。
    - `samesite=lax`：跨站发起的写请求不带这张 Cookie，是 CSRF 的第一道防线。
    - `secure`：仅在 HTTPS 下发送，由 `AUTH_COOKIE_SECURE` 控制——本地开发是 http，
      强制 secure 会导致登录后立刻掉线，因此默认关、部署到 HTTPS 时打开。
    """
    response.set_cookie(
        key=session_cookie.COOKIE_NAME,
        value=token,
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """退出登录：删除 Cookie。服务端那一行由调用方另行删除。"""
    response.delete_cookie(key=session_cookie.COOKIE_NAME, path="/")
