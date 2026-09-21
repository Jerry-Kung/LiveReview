"""用户鉴权接口（V0.5）：登录、退出、当前身份与登录后的运行配置状态。

无注册接口：账号存在数据库的 `users` 表里，初始化用
`python -m app.auth.init_admin` 创建（见 `app/auth/init_admin.py`）。

登录失败一律返回同一句话，不区分「账号不存在」与「口令不正确」——区分开等于把
「哪些账号存在」白送给对方。具体原因只写服务端日志。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser,
    clear_session_cookie,
    read_session_token,
    require_user,
    set_session_cookie,
)
from app.auth import store
from app.auth.service import authenticate, get_throttle
from app.config import Settings, get_settings
from app.database import check_database_ok, get_db
from app.schemas import (
    LoginRequest,
    SessionResponse,
    StatusResponse,
    UserResponse,
)
from app.storage import build_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 对外统一的失败文案：不透露账号是否存在
LOGIN_FAILED_DETAIL = "账号或密码不正确"
# 触发节流时的文案：说清「稍后再试」，避免用户以为是自己记错了口令
THROTTLED_DETAIL = "登录尝试过于频繁，请稍后再试"
# 一个账号都没有时的文案：这不是登录失败，而是部署还没做完，必须说清下一步做什么
NO_ACCOUNT_DETAIL = "服务端尚未创建任何账号，请先在服务端执行 python -m app.auth.init_admin"


def _storage_state(settings: Settings) -> tuple[str, list[str]]:
    """与健康检查同口径的存储状态：configured / not_configured / unavailable。"""
    if not settings.storage_configured:
        return "not_configured", settings.storage_missing_fields
    try:
        build_storage(settings)
    except Exception as exc:  # noqa: BLE001 —— 状态查询不应因存储异常中断
        logger.error("对象存储初始化失败：%s", exc)
        return "unavailable", []
    return "configured", []


@router.post("/login", response_model=SessionResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SessionResponse:
    # 一个账号都没有时明确报「没建号」，而不是让每次登录都变成「口令不对」：
    # 后者会让人反复怀疑是密码记错了，而真正要做的是一次初始化脚本
    if store.count_users(db) == 0:
        logger.warning("登录被拒绝：数据库中没有任何账号")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NO_ACCOUNT_DETAIL)

    username = payload.username.strip()
    result = authenticate(db, username, payload.password, throttle=get_throttle())
    if not result.ok:
        if result.reason == "throttled":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=THROTTLED_DETAIL
            )
        logger.info("登录失败（%s）：账号=%s", result.reason, username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_FAILED_DETAIL)

    user = store.get_user_by_id(db, result.user.user_id)
    if user is None:
        # 比对通过到写入会话之间账号被删掉：极窄的竞态，按未登录处理即可
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_FAILED_DETAIL)
    issued = store.create_session(
        db,
        user,
        ttl_seconds=settings.auth_session_ttl_seconds,
        absolute_ttl_seconds=settings.auth_session_absolute_ttl_seconds,
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookie(response, settings, issued.token)
    logger.info("登录成功：账号=%s", result.user.username)
    return SessionResponse(
        user=UserResponse(username=result.user.username, display_name=result.user.display_name)
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Response:
    """退出登录：删除服务端会话记录，并清掉 Cookie。

    与 V0.5 的差别在这里：那时只是让浏览器丢掉 Cookie，服务端不存任何东西，因此
    「已登出」只对当前这台设备成立——令牌若被抄走仍然可用。现在这张会话在库里被真的
    删掉，旧令牌再也换不到任何东西。
    无需登录即可调用，重复调用也无副作用（第二次删不到记录，照样返回 204）。

    返回注入进来的 `response` 本身，而不是新建一个：新对象会把这里设下的
    `Set-Cookie` 丢掉，表现为「点了退出但下一次请求仍然是登录态」。
    """
    store.delete_session(db, read_session_token(request))
    clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/session", response_model=SessionResponse)
def read_session(current: CurrentUser = Depends(require_user)) -> SessionResponse:
    """当前登录身份：前端启动时用它判断该显示登录页还是工作台。"""
    return SessionResponse(
        user=UserResponse(username=current.username, display_name=current.display_name)
    )


@router.get("/status", response_model=StatusResponse)
def read_status(
    current: CurrentUser = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> StatusResponse:
    """登录后可见的运行配置状态。

    对象存储与模型的缺失项名称原先挂在公开的 `/health` 上，V0.5 起移到这里：编排探针
    只需要「服务是否可用」，「用了哪个桶、哪家模型、缺哪个密钥」不该对未登录者公开。
    """
    storage_state, storage_missing = _storage_state(settings)
    account_count, account_problem = settings.auth_account_state()
    return StatusResponse(
        version=settings.app_version,
        environment=settings.app_env,
        database="ok" if check_database_ok() else "degraded",
        storage=storage_state,
        storage_missing=storage_missing,
        llm="configured" if settings.llm_configured else "not_configured",
        llm_missing=settings.llm_missing_fields,
        auth_users=account_count,
        auth_warnings=[account_problem] if account_problem else [],
    )
