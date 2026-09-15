"""健康检查路由：供编排探针与前端调用。"""

import logging

from fastapi import APIRouter

from app.config import get_settings
from app.database import check_database_ok
from app.storage import build_storage

logger = logging.getLogger(__name__)

router = APIRouter()


def _storage_state(settings) -> tuple[str, list[str]]:
    """返回 (存储状态, 缺失配置项)。

    configured：凭据齐全且实现可构造；
    not_configured：缺必填项，服务仍可用（V0.1.2 尚不依赖存储）；
    unavailable：已配置但构造失败，整体服务应视为降级。
    """
    if not settings.storage_configured:
        return "not_configured", settings.storage_missing_fields
    try:
        build_storage(settings)
    except Exception as exc:  # noqa: BLE001 —— 探针不应因存储异常中断
        logger.error("对象存储初始化失败：%s", exc)
        return "unavailable", []
    return "configured", []


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    db_ok = check_database_ok()
    storage_state, storage_missing = _storage_state(settings)
    healthy = db_ok and storage_state != "unavailable"
    return {
        "status": "ok" if healthy else "degraded",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "database": "ok" if db_ok else "degraded",
        "storage": storage_state,
        "storage_missing": storage_missing,
    }
