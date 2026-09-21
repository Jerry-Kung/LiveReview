"""上传与任务链路的公共测试夹具：数据库、存储、配置与客户端装配。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.service import LoginThrottle
from app.config import Settings, get_settings
from app.database import get_db
from app.llm import use_client
from app.models import Base
from app.storage import InMemoryStorage, StorageConfig
from tests.fake_llm import FakeReviewClient, FakeUnderstandingClient

CHUNK_SIZE = 1024 * 1024
FILE_SIZE = 3 * CHUNK_SIZE  # 3 片，便于构造缺片与乱序场景

# 测试账号与口令：V0.5 起业务路由全部要求登录，各链路测试统一用这一组凭据
TEST_USERNAME = "tester"
TEST_PASSWORD = "test-password-1"
TEST_DISPLAY_NAME = "测试用户"
# 哈希迭代次数压到最小：真实取值是 24 万次，测试里每条用例都跑一遍只会拖慢套件
TEST_HASH_ITERATIONS = 1_000


def hash_for_test(password: str) -> str:
    """生成测试用口令哈希：迭代次数取最小值，避免拖慢整套用例。"""
    from app.auth.password import hash_password

    return hash_password(password, iterations=TEST_HASH_ITERATIONS)


@pytest.fixture(autouse=True)
def fast_password_hashing(request, monkeypatch):
    """把建号与登录用的口令哈希迭代次数压到最小。

    V0.5.1 起账号在数据库里，每条用例都可能建号；真实取值 24 万次在测试里只是白等
    几十毫秒。生产取值由 `DEFAULT_ITERATIONS` 决定，另有单测钉住它——那条用例标了
    `original`，这里据此跳过打桩，否则它会读到被压小的值而假失败。
    """
    if "original" in request.keywords:
        return
    monkeypatch.setattr("app.auth.password.DEFAULT_ITERATIONS", TEST_HASH_ITERATIONS)


def fake_ffprobe_args() -> list[str]:
    """假 ffprobe 的命令行前缀：本地不部署 FFmpeg，用脚本替代以覆盖完整链路。

    以「解释器 + 脚本」的形式给出，Windows 与 POSIX 都能执行，不需要 shebang 与执行位。
    """
    script = Path(__file__).with_name("fake_ffprobe.py")
    return [sys.executable, str(script)]


def fake_ffmpeg_args() -> list[str]:
    """假 ffmpeg 的命令行前缀：覆盖转封装与切分的编排，不涉及真实编解码。"""
    script = Path(__file__).with_name("fake_ffmpeg.py")
    return [sys.executable, str(script)]


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
    # ffprobe/ffmpeg 指向假实现：本机不部署 FFmpeg，但处理链路仍需被完整覆盖
    # 模型配置给一组假值：识别链路的编排无需真实凭据即可覆盖
    return Settings(
        _env_file=None,
        media_root=str(media_root),
        upload_chunk_size=CHUNK_SIZE,
        ffprobe_path=fake_ffprobe_args(),
        ffmpeg_path=fake_ffmpeg_args(),
        llm_base_url="https://llm.test/api/v3",
        llm_api_key="test-api-key",
        llm_model_name="test-model",
        llm_timeout_seconds=30,
        llm_max_attempts=2,
    )


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

    `submit` 接受与真实执行器相同的 `(task_id, phase)` 参数，使识别阶段的入队同样
    在本进程内同步完成。
    """

    def submit(self, fn, task_id: str, phase: str = "ingest"):
        from concurrent.futures import Future

        from fastapi import HTTPException

        future: Future = Future()
        try:
            fn(task_id, phase)
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
def test_user(db_session_factory):
    """数据库里的测试账号。V0.5.1 起账号存在 `users` 表，登录前必须先有一条。"""
    from app.auth import store

    db = db_session_factory()
    try:
        return store.create_user(
            db,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
            display_name=TEST_DISPLAY_NAME,
        )
    finally:
        db.close()


@pytest.fixture
def client(db_session_factory, test_settings: Settings, storage, test_user, monkeypatch) -> TestClient:
    """装配测试客户端：库、存储与后台执行器全部替换为可控实现。

    V0.5 起业务接口要求登录，这里在装配完成后自动登录一次，使既有链路的测试不必
    各自重复登录动作；验证「未登录会被拦住」的用例请用 `anonymous_client`。
    """
    from app.main import app
    from app import uploads as uploads_module

    def override_get_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: test_settings
    # 鉴权依赖在函数内导入配置工厂（模块顶层互相导入会成环），因此要替换的是
    # `app.config` 里那个名字：它才是 `_settings()` 每次调用时真正去取的对象
    monkeypatch.setattr("app.config.get_settings", lambda: test_settings)
    # 鉴权依赖与登录接口自己开数据库会话（不走 `get_db`，见 `app/auth/__init__.py`），
    # 因此会话工厂也要指向测试库，否则它们会连到本机的 data/liverreview.db 上
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)
    # 各业务模块用 `from app.storage import get_storage` 直接绑定，因此按模块逐个替换
    monkeypatch.setattr("app.storage.get_storage", lambda: storage)
    monkeypatch.setattr("app.deletion.get_storage", lambda: storage)
    monkeypatch.setattr("app.tasks.runner.get_storage", lambda: storage)
    # 上传入库链路（合并 → 上传对象存储）已从路由层抽到 `app.uploads`，存储实例在那边取
    monkeypatch.setattr(uploads_module, "get_storage", lambda: storage)
    # 关掉线程池：任务在本进程内同步执行，测试无需等待后台线程
    monkeypatch.setattr("app.tasks.executor._get_executor", lambda: _SyncExecutor())
    monkeypatch.setattr("app.tasks.executor.SessionLocal", db_session_factory)
    # 任务体不走依赖注入，直接取配置单例；测试里指向假 ffprobe，避免依赖本机安装 FFmpeg
    monkeypatch.setattr("app.tasks.runner.get_settings", lambda: test_settings)
    # 识别链路的配置与存储同样走单例；存储已替换为 Mock，配置指向测试配置
    monkeypatch.setattr("app.tasks.understanding.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.tasks.understanding.get_storage", lambda: storage)
    # 复盘链路的配置同样走单例（它不碰对象存储，输入只有已落库的识别结果）
    monkeypatch.setattr("app.tasks.review.get_settings", lambda: test_settings)
    # 识别客户端默认替换为假实现，绝不发真实网络请求；单测内可再替换成自己的实例
    monkeypatch.setattr(
        "app.tasks.understanding.get_understanding_client", lambda: FakeUnderstandingClient()
    )
    monkeypatch.setattr("app.tasks.review.get_review_client", lambda: FakeReviewClient())
    # 节流按账号在进程内存里计数，用例之间必须隔离，否则前一条用例的失败次数会累积
    monkeypatch.setattr("app.auth.service._throttle", LoginThrottle())

    with TestClient(app) as test_client:
        login(test_client)
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def use_settings(monkeypatch, test_settings: Settings):
    """在测试内替换应用读到的配置。

    走 `dependency_overrides` 而不是 monkeypatch 模块属性：鉴权依赖从依赖注入取配置，
    只改某个模块里的函数引用不会影响它——那会变成一条「看起来改了、其实没生效」的测试。
    """

    def apply(**changes):
        from app.main import app

        updated = test_settings.model_copy(update=changes)
        app.dependency_overrides[get_settings] = lambda: updated
        monkeypatch.setattr("app.config.get_settings", lambda: updated)
        # 任务体不走依赖注入，直接取配置单例，替换配置时一并指向新值
        monkeypatch.setattr("app.tasks.runner.get_settings", lambda: updated)
        monkeypatch.setattr("app.tasks.understanding.get_settings", lambda: updated)
        monkeypatch.setattr("app.tasks.review.get_settings", lambda: updated)
        return updated

    return apply


@pytest.fixture
def anonymous_client(client: TestClient) -> TestClient:
    """未登录的客户端：与 `client` 同一个应用实例，但清掉了会话 Cookie。"""
    client.cookies.clear()
    return client


def login(client: TestClient, username: str = TEST_USERNAME, password: str = TEST_PASSWORD):
    """登录并让客户端持有会话 Cookie。"""
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response


@pytest.fixture
def model(monkeypatch) -> FakeReviewClient:
    """注入假复盘客户端并返回它，供断言调用次数与入参。

    复盘链路用 `from app.llm import ...` 直接绑定函数名，因此补丁要打在**使用点**上
    （`app.tasks.review`），打在 `app.llm` 上不会生效。

    放在 conftest 而非 test_review.py：鉴权用例也要跑通「识别 → 复盘」才能验证下载口，
    两处各写一份同样的夹具会让「补丁打在哪里」这条例外知识分叉。
    """
    fake = FakeReviewClient()
    monkeypatch.setattr("app.tasks.review.get_review_client", lambda: fake)
    return fake
