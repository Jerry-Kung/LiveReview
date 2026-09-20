"""用户鉴权接口（V0.5）：登录、退出、当前身份与登录后的运行配置状态。

无注册接口：初始化用户由 `AUTH_USERS` 环境变量手工配置，口令哈希用
`python -m app.auth.passwd` 生成（见 `app/auth/config.py`）。

登录失败一律返回同一句话，不区分「账号不存在」与「口令不正确」——区分开等于把
「哪些账号存在」白送给对方。具体原因只写服务端日志。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth import (
    CurrentUser,
    clear_session_cookie,
    require_user,
    set_session_cookie,
)
from app.auth.config import AuthConfigError
from app.auth.service import authenticate, get_throttle
from app.config import Settings, get_settings
from app.database import check_database_ok
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
    response: Response,
    settings: Settings = Depends(get_settings),
) -> SessionResponse:
    try:
        users = settings.auth_user_map
    except AuthConfigError as exc:
        # 配置写坏时明确报错：让问题停在启动日志与本次响应上，而不是变成「怎么都登不上」
        logger.error("AUTH_USERS 配置无效：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="服务端用户配置无效，请联系管理员",
        ) from exc

    if not users:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务端尚未配置任何可登录账号",
        )

    username = payload.username.strip()
    result = authenticate(users, username, payload.password, throttle=get_throttle())
    if not result.ok:
        if result.reason == "throttled":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=THROTTLED_DETAIL
            )
        logger.info("登录失败（%s）：账号=%s", result.reason, username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_FAILED_DETAIL)

    set_session_cookie(response, settings, result.user.username)
    logger.info("登录成功：账号=%s", result.user.username)
    return SessionResponse(
        user=UserResponse(username=result.user.username, display_name=result.user.display_name)
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> Response:
    """退出登录：删除会话 Cookie。无需登录即可调用，重复调用也无副作用。

    返回注入进来的 `response` 本身，而不是新建一个：新对象会把这里设下的
    `Set-Cookie` 丢掉，表现为「点了退出但下一次请求仍然是登录态」。
    """
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
    return StatusResponse(
        version=settings.app_version,
        environment=settings.app_env,
        database="ok" if check_database_ok() else "degraded",
        storage=storage_state,
        storage_missing=storage_missing,
        llm="configured" if settings.llm_configured else "not_configured",
        llm_missing=settings.llm_missing_fields,
        auth_users=len(settings.auth_user_map),
        auth_session_secret_configured=settings.auth_session_secret_configured,
        auth_warnings=settings.auth_warnings,
    )
