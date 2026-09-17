"""数据库建表与补列的测试。

背景：V0 阶段表结构随版本演进，`create_all` 只建缺失的表、不改已存在的表。
一旦旧库缺列，启动阶段就会以 `no such column` 崩溃（V0.1.5 在测试环境真实发生过），
因此补列逻辑本身要有独立覆盖：旧库能升级、老数据不丢、重复启动不出错。
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database as database_module
from app.models import Base


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "liverreview.db"


@pytest.fixture
def engine(db_path, monkeypatch):
    """独立的文件型 SQLite：补列是 DDL，必须落在真实文件上才可验证。"""
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database_module, "engine", engine)
    yield engine
    engine.dispose()


def _columns(db_path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _tables(db_path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _model_columns(table: str) -> set[str]:
    return {column.name for column in Base.metadata.tables[table].columns}


def _drop_v0_1_5_columns(db_path) -> None:
    """把库降级成 V0.1.4 的形态：删掉 V0.1.5 新增的列与表。"""
    with sqlite3.connect(db_path) as conn:
        for column in (
            "split_source_format",
            "split_source_duration_seconds",
            "converted_at",
            "coverage_issues_json",
            "split_checked_at",
        ):
            conn.execute(f'ALTER TABLE tasks DROP COLUMN "{column}"')
        conn.execute("DROP TABLE media_clips")


def test_init_db_creates_all_tables_and_columns(engine, db_path):
    database_module.init_db()
    assert _tables(db_path) >= {"upload_sessions", "tasks", "media_clips"}
    assert _columns(db_path, "tasks") == _model_columns("tasks")
    assert _columns(db_path, "media_clips") == _model_columns("media_clips")


def test_init_db_adds_missing_columns_to_existing_table(engine, db_path):
    database_module.init_db()
    _drop_v0_1_5_columns(db_path)
    assert "split_source_format" not in _columns(db_path, "tasks")

    database_module.init_db()

    # 补列后与模型完全一致，且新表也一并建出
    assert _columns(db_path, "tasks") == _model_columns("tasks")
    assert "media_clips" in _tables(db_path)


def test_init_db_preserves_existing_rows(engine, db_path):
    database_module.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, kind, status, progress, created_at, updated_at) "
            "VALUES ('t-1', 'ingest', 'uploaded', 40, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')"
        )
    _drop_v0_1_5_columns(db_path)

    database_module.init_db()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, status, split_source_format FROM tasks").fetchall()
    assert rows == [("t-1", "uploaded", None)]  # 老行仍在，新列取空值


def test_sync_missing_columns_is_idempotent(engine, db_path):
    """首次建库时 create_all 已把列建全，因此补列阶段应当无列可补。"""
    database_module.init_db()
    assert database_module.sync_missing_columns() == []
    assert database_module.sync_missing_columns() == []


def test_sync_missing_columns_reports_added_columns(engine, db_path):
    database_module.init_db()
    _drop_v0_1_5_columns(db_path)

    added = database_module.sync_missing_columns()

    assert sorted(added) == sorted(
        f"tasks.{name}"
        for name in (
            "split_source_format",
            "split_source_duration_seconds",
            "converted_at",
            "coverage_issues_json",
            "split_checked_at",
        )
    )
    assert database_module.sync_missing_columns() == []  # 补过一次后不再重复补


def test_init_db_then_query_works_on_upgraded_database(engine, db_path):
    """回归用例：升级后的库必须能跑通启动阶段的那条查询。

    原故障正是 `requeue_pending` 触发的 `list_tasks`，补列没覆盖全就会在这里复现。
    """
    from app.tasks.runner import list_tasks

    database_module.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, kind, status, progress, created_at, updated_at) "
            "VALUES ('t-2', 'ingest', 'uploaded', 0, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')"
        )
    _drop_v0_1_5_columns(db_path)

    database_module.init_db()

    session = sessionmaker(bind=engine)()
    try:
        assert [task.id for task in list_tasks(session)] == ["t-2"]
    finally:
        session.close()


def test_sync_missing_columns_rejects_column_that_cannot_be_backfilled(engine, monkeypatch):
    """既不可空又无默认值的列无法在保留数据的前提下补上，必须明确报错而非静默跳过。"""
    from sqlalchemy import Column, Integer, MetaData, String, Table

    database_module.init_db()
    metadata = MetaData()
    Table(
        "tasks",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("required_new_column", Integer, nullable=False),  # 缺列且无法回填
    )

    class _MetadataProxy:
        """只替换 Base.metadata，避免动到其它测试共享的全局 Base。"""

        def __init__(self, metadata):
            self.metadata = metadata

    monkeypatch.setattr("app.models.Base", _MetadataProxy(metadata))

    with pytest.raises(RuntimeError, match="required_new_column"):
        database_module.sync_missing_columns()

    # 报错前不做任何半途改动：无法补齐的列不会被加上
    assert "required_new_column" not in _columns(engine.url.database, "tasks")
