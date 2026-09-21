"""健康检查路由：供编排探针与前端调用。

刻意保持**公开**：容器编排的 healthcheck 不能先登录。因此这里只回答一件事——服务与它
的必需依赖是否可用，不暴露运行细节。

V0.5 起收敛了回显内容：对象存储与模型的配置状态、缺失项名称原先挂在这里，现在移到
登录后的 `/api/auth/status`。「用了哪个模型、缺哪个密钥」对未登录者没有用处，却是一份
现成的情报。存储可用性本身仍需参与整体状态判定，因为它影响业务能否跑通。
"""

import logging

from fastapi import APIRouter

from app.config import get_settings
from app.database import check_database_ok
from app.storage import build_storage

logger = logging.getLogger(__name__)

router = APIRouter()


def _storage_available(settings) -> bool:
    """对象存储是否可用：未配置视为可用（回落 Mock，上传链路仍能验证），构造失败为不可用。"""
    if not settings.storage_configured:
        return True
    try:
        build_storage(settings)
    except Exception as exc:  # noqa: BLE001 —— 探针不应因存储异常中断
        logger.error("对象存储初始化失败：%s", exc)
        return False
    return True


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    db_ok = check_database_ok()
    storage_ok = _storage_available(settings)
    return {
        "status": "ok" if (db_ok and storage_ok) else "degraded",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "database": "ok" if db_ok else "degraded",
        # 只给可用性，不给存储类型、桶名与缺失项名称
        "storage": "ok" if storage_ok else "unavailable",
    }
