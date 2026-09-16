"""上传与任务链路的公共测试夹具：数据库、存储、配置与客户端装配。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.database import get_db
from app.models import Base
from app.storage import InMemoryStorage, StorageConfig

CHUNK_SIZE = 1024 * 1024
FILE_SIZE = 3 * CHUNK_SIZE  # 3 片，便于构造缺片与乱序场景


@pytest.fixture
def db_session_factory(tmp_path: Path):
    """文件型 SQLite：后台执行线程需要看到接口线程已提交的数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    return tmp_path / "media"


@pytest.fixture
def test_settings(media_root: Path) -> Settings:
    # _env_file=None：不读本机 .env，避免本地凭据影响测试结果
    return Settings(_env_file=None, media_root=str(media_root), upload_chunk_size=CHUNK_SIZE)


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage(
        StorageConfig(
            endpoint="mock.local",
            region="mock",
            bucket="mock-bucket",
            access_key="ak",
            secret_key="sk",
            object_prefix="liverreview/",
        )
    )


class _SyncExecutor:
    """同步执行器：submit 立即在当前线程执行，等价于无线程池的后台执行。

    与线程池一致地吞掉任务体的异常（失败原因已落库），但不吞 `HTTPException`：
    入口函数自身抛出的 HTTP 错误必须照常返回给测试。
    """

    def submit(self, fn, task_id: str):
        from concurrent.futures import Future

        from fastapi import HTTPException

        future: Future = Future()
        try:
            fn(task_id)
        except HTTPException:
            raise
        except BaseException as exc:  # noqa: BLE001 —— 与线程池一致：异常不向调用方抛出
            future.set_exception(exc)
        else:
            future.set_result(None)
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        return None


@pytest.fixture
def client(db_session_factory, test_settings: Settings, storage, monkeypatch) -> TestClient:
    """装配测试客户端：库、存储与后台执行器全部替换为可控实现。"""
    from app.main import app
    from app.routers import uploads as uploads_module

    def override_get_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: test_settings
    monkeypatch.setattr(uploads_module, "get_storage", lambda: storage)
    # 关掉线程池：任务在本进程内同步执行，测试无需等待后台线程
    monkeypatch.setattr("app.tasks.executor._get_executor", lambda: _SyncExecutor())
    monkeypatch.setattr("app.tasks.executor.SessionLocal", db_session_factory)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
