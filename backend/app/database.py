"""数据库接线：SQLite 引擎、会话工厂与建表。"""

import logging
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# 确保 SQLite 本地数据库的父目录存在
if settings.database_url.startswith("sqlite:///"):
    db_path = Path(settings.database_url.replace("sqlite:///", ""))
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """建立缺失的表结构。

    V0 验证阶段表结构仍在演进，用 `create_all` 保持轻量；已存在的表不会被改动，
    因此新增字段需在测试环境重建数据库文件（见部署文档）。
    """
    from app.models import Base

    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI 依赖：每个请求一个会话，请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database_ok() -> bool:
    """探测数据库连通性，异常时返回 False 而不抛出。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("数据库连通性检查失败")
        return False
