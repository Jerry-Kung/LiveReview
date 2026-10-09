"""设置接口（V0.7.0）：读取与修改系统级可变配置。

本版只有一个开关——全自动流程模式。两条对外约定：

1. **读对所有登录账号开放，写只对管理员**。上传页要按开关切换使用说明的文案（那是对所有人
   都成立的事实），因此普通账号必须读得到；而「让整台机器上的每次上传都开始花模型的钱」是
   一个影响所有人的决定，与账号管理同级，只对管理员开放。判定沿用 `require_admin`：非管理员
   拿 403（已登录、只是不对他开放），不是 401。
2. **写入立即对新上传生效，不追溯已有任务**。任务的 `auto_run` 在创建时取值写死，因此改开关
   不会改变任何已存在任务的行为（见 `docs/specs/v0.7.0-design.md` §3.5）。

读取的默认值（V0.7.1 起）是**开启**：库里还没有那一行时返回真，因此新装环境不必先去点一次
开关，上传完的任务就会自己跑完。

响应里带上会影响这条链路的运行参数（切片时长与体积上限、模型名）。这些取值目前由环境变量
决定、界面上改不了，但用户需要知道打开开关意味着「上传完就开始按这个模型、这个切片粒度花钱」。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth import CurrentUser, require_admin, require_user
from app.config import Settings, get_settings
from app.database import get_db
from app.schemas import SettingsResponse, SettingsUpdateRequest
from app.settings_store import read_auto_pipeline, write_auto_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _to_response(settings: Settings, *, auto_pipeline: bool) -> SettingsResponse:
    return SettingsResponse(
        auto_pipeline=auto_pipeline,
        # 只回显模型名，不回显 base_url 与 api_key（与任务接口同一口径）
        model_name=settings.llm_model_name if settings.llm_configured else None,
        llm_configured=settings.llm_configured,
        split_max_duration_seconds=settings.split_max_duration_seconds,
        split_max_clip_bytes=settings.split_max_clip_bytes,
        video_ttl_seconds=settings.storage_video_ttl_seconds,
    )


@router.get("", response_model=SettingsResponse)
def read_settings(
    current: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SettingsResponse:
    """当前系统设置。所有登录账号可读：上传页的文案要据此切换。"""
    return _to_response(settings, auto_pipeline=read_auto_pipeline(db))


@router.patch("", response_model=SettingsResponse)
def update_settings(
    payload: SettingsUpdateRequest,
    current: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SettingsResponse:
    """修改系统设置。只对管理员开放。

    用 `PATCH` 而不是 `PUT`：请求体里只带要改的字段，未提供的字段保持原值。`PUT` 的语义是
    「整体替换」，日后新增配置项时，一个不带上新字段的旧客户端会把它们清成默认值。
    """
    enabled = payload.auto_pipeline
    write_auto_pipeline(db, enabled=enabled, updated_by=current.username)
    logger.info("管理员 %s 将全自动流程模式设为 %s", current.username, "开启" if enabled else "关闭")
    # 回读一次而不是直接用入参：响应必须反映真正落库的取值，否则用例与界面都会替代码「记得」
    # 写入成功了什么
    return _to_response(settings, auto_pipeline=read_auto_pipeline(db))
