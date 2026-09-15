"""健康检查路由：供编排探针与前端调用。"""

from fastapi import APIRouter

from app.config import get_settings
from app.database import check_database_ok

router = APIRouter()


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    db_ok = check_database_ok()
    return {
        "status": "ok" if db_ok else "degraded",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "database": "ok" if db_ok else "degraded",
    }
