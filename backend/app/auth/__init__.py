"""鉴权对外入口：FastAPI 依赖、Cookie 读写与 CSRF 底线。

这一层是「谁能访问」的唯一判定点，业务路由只依赖 `require_user`，不自己解析 Cookie。

两道防线：

1. **登录态**：`require_user` 从 Cookie 取令牌，到 `sessions` 表查证并检查有效期，
   未通过一律 401。令牌无效、会话过期、账号已删除走同一条拒绝路径。
2. **CSRF**：写操作（非 GET/HEAD/OPTIONS）校验 `Origin` 是否与请求自身的 Host 同源。
   Cookie 是浏览器自动携带的凭证，`SameSite=Lax` 已能挡住跨站的表单与 fetch，
   但同站子域或放宽策略时仍会被利用，因此对写操作再加一条同源检查。
   没有 `Origin` 的请求放行——那是非浏览器客户端（curl、脚本）发出的，它们本就不带 Cookie。
   反向代理会改写 `Host`（`$host` 不含端口），因此同时比对 `X-Forwarded-Host`，
   否则经 nginx 进来的正常写请求会被判成跨站。

会话查证每次请求都要读一次库（V0.5 的签名票据不需要），这是把会话收进数据库的代价，
换来的是「能单独撤销一张会话」。SQLite 上的主键查询在这个量级不构成瓶颈。

V0.5.2 在这两道防线之上加了第三层：`require_admin`。它只区分「能不能管账号」，
不构成通用权限体系——本版除账号管理外的功能对所有登录账号一视同仁。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response, status

from app.auth import session as session_cookie
from app.auth import store
from app.models import ROLE_ADMIN

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


def _request_hosts(request: Request) -> list[str]:
    """本请求可能被访问到的 Host，按可信度排列。

    取 `Host` 之外还要取 `X-Forwarded-Host`：反向代理常见的写法是
    `proxy_set_header Host $host`，`$host` 不含端口，于是浏览器发来的
    `Origin: http://<主机>:<端口>` 与后端看到的 `Host: <主机>` 对不上，正常登录会被
    判成跨站。这两种取值都只在「同一部署的前端与后端」之间出现，逐条比对不会放宽判定。
    """
    values: list[str] = []
    for header in ("host", "x-forwarded-host"):
        raw = request.headers.get(header)
        if not raw:
            continue
        # 逐跳代理会在 X-Forwarded-Host 里追加，取第一个（最靠近客户端的那一跳）
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    return values


def _hostname_port(netloc: str) -> tuple[str, str] | None:
    """把 `主机:端口` 拆成 (主机, 端口)；端口缺省时返回空串。

    不用 `parsed.port`：那是整数，`http://x` 与 `http://x:80` 都会得到 80，等于把
    「没写端口」与「显式写了默认端口」混为一谈，而这正是要区分的地方。
    """
    if not netloc:
        return None
    hostname, _, port = netloc.rpartition(":")
    if not hostname:  # 没有冒号，全是主机名
        return netloc.lower(), ""
    return hostname.lower(), port


def _host_matches(candidate_netloc: str, origin_host: tuple[str, str]) -> bool:
    """本请求的一个 Host 取值是否算作与该 Origin 同源。

    主机名必须一致；端口只有在本请求的 Host 里写了才比对——`Host` 不带端口说明请求
    正是从这个地址的默认端口进来的（反向代理的 `$host` 就是这样），端口这一段无从比较，
    按同源处理。nginx 已改为转发 `$http_host` 保留端口，这里是它之外的一道兜底：
    外层还有别的网关、或它也吃掉了端口时，正常登录不至于被判成跨站。
    """
    candidate = _hostname_port(candidate_netloc)
    if candidate is None:
        return False
    hostname, port = candidate
    if hostname != origin_host[0]:
        return False
    return port == "" or port == origin_host[1]


def _is_same_origin(request: Request) -> bool:
    """请求的 Origin 是否与本请求的 Host 同源。

    Origin 缺失即放行（见模块说明）。协议不参与比对——`Origin` 里的协议由浏览器按页面
    实际协议给出，后端经反向代理后看到的请求协议无法可靠对应；主机名必须一致，端口按
    `_host_matches` 的规则处理。
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    candidates = _request_hosts(request)
    if not candidates or not parsed.netloc:
        return False
    origin_host = _hostname_port(parsed.netloc)
    if origin_host is None:
        return False
    return any(_host_matches(candidate, origin_host) for candidate in candidates)


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


def require_admin(current: CurrentUser = Depends(require_user)) -> CurrentUser:
    """路由依赖：在「已登录」之上再要求管理员角色（V0.5.2）。

    写成 `require_user` 的子依赖而不是自己再解析一次 Cookie：FastAPI 会缓存同一请求内
    的依赖结果，因此同源校验与会话查库在一次请求里仍然只做一遍，handler 里无论写
    `Depends(require_user)` 还是 `Depends(require_admin)` 都不会多查一次库。

    403 而不是 401：调用者是**已登录**的合法用户，只是这件事不对他开放。回 401 会让
    前端把它当成「会话失效」而把人弹回登录页，而他重新登录后依然没有权限。
    """
    if current.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有管理员账号可以执行此操作",
        )
    return current


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
