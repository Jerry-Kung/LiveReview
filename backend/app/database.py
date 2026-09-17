"""数据库接线：SQLite 引擎、会话工厂与建表。"""

import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.compiler import DDLCompiler

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
    """建立缺失的表结构，并补齐已存在表中新增的列。

    V0 验证阶段表结构仍在演进，用 `create_all` 保持轻量：它只建缺失的表，
    已存在的表原样保留。因此每次新增字段后，旧数据库会缺列并在查询时报
    `no such column`（启动阶段就会因 `requeue_pending` 直接崩掉）。这里在
    `create_all` 之后补一次「加列」，让旧库能直接升级，不必手动删库重建。

    补列只做加法：新增列一律是「可空或有默认值」，`ALTER TABLE ADD COLUMN`
    能安全地在有数据的表上执行；已有的列不改类型、不改可空性、不删除，避免
    任何一次启动改动既有数据。真正需要改列语义时仍须重建数据库（见部署文档）。
    """
    from app.models import Base

    Base.metadata.create_all(bind=engine)
    sync_missing_columns()


def sync_missing_columns() -> list[str]:
    """把模型里有、库里没有的列补进库，返回补列描述（无变化时为空列表）。

    列类型与默认值直接取自模型定义，不在这里维护第二份清单，避免模型与
    补列逻辑各自漂移。
    """
    from app.models import Base

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    # 先一次性取全部表的列：inspect() 带缓存，加列之后再问同一实例会拿到旧快照
    existing_columns = {
        name: {column["name"] for column in inspector.get_columns(name)}
        for name in existing_tables
    }
    added: list[str] = []
    pending: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            present = existing_columns.get(table.name)
            if present is None:
                continue  # 新表由 create_all 建好，列自然齐全
            for column in table.columns:
                if column.name in present:
                    continue
                definition = _column_definition(column)
                if definition is None:
                    pending.append(f"{table.name}.{column.name}")
                    continue
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {definition}')
                added.append(f"{table.name}.{column.name}")

    if added:
        logger.info("数据库补列完成：%s", "、".join(added))
    if pending:
        # 不能默默跳过：缺列会在后续查询里以更晦涩的方式爆掉
        raise RuntimeError(
            "以下新增字段既不可空又无默认值，无法在保留现有数据的前提下补齐，"
            f"请重建数据库：{'、'.join(pending)}"
        )
    return added


def _column_definition(column) -> str | None:
    """渲染 `ALTER TABLE ... ADD COLUMN` 的列定义；无法安全补齐时返回 None。

    SQLite 的 ADD COLUMN 不允许无默认值的 NOT NULL（已有行填不出来），
    这类列交给调用方报错，不做「先加列再猜默认值」的妥协。
    """
    default = column.default
    has_literal_default = default is not None and not default.is_callable
    if not column.nullable and not has_literal_default:
        return None

    compiler = DDLCompiler(engine.dialect, None)
    # 用 type_compiler 取类型名：`get_column_specification` 会连带列名，不能直接复用
    spec = compiler.type_compiler.process(column.type, type_expression=column)
    if not column.nullable:
        spec += " NOT NULL"
    if has_literal_default:
        value = default.arg
        if isinstance(value, str):
            spec += f" DEFAULT '{value}'"
        elif isinstance(value, bool):
            spec += f" DEFAULT {1 if value else 0}"
        else:
            spec += f" DEFAULT {value}"
    return f"{compiler.preparer.quote(column.name)} {spec}"


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
