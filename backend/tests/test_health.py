"""健康检查契约：公开可达、只回可用性、不回运行细节。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert data["service"] == "LiveReview"
    assert data["version"] == "0.5.1"
    assert data["environment"] == "development"
    assert data["database"] in ("ok", "degraded")
    assert data["storage"] in ("ok", "unavailable")


def test_health_does_not_expose_runtime_details():
    """未登录也能访问，因此这里不能出现模型型号、桶名与缺失项名称。"""
    data = client.get("/health").json()
    for leaked in ("storage_missing", "llm", "llm_missing"):
        assert leaked not in data


def test_health_stays_ok_without_storage_credentials(monkeypatch):
    """缺凭据时存储回落 Mock，上传链路仍可用，整体状态不降级。"""
    from app.config import Settings
    from app.routers import health as health_module

    for name in ("TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_ENDPOINT", "TOS_REGION", "TOS_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(health_module, "get_settings", lambda: settings)
    monkeypatch.setattr(health_module, "check_database_ok", lambda: True)

    data = client.get("/health").json()
    assert data["storage"] == "ok"
    assert data["status"] == "ok"


def test_health_reports_storage_unavailable_when_build_fails(monkeypatch):
    """已配置存储但构造失败：整体降级，探针据此重启容器。"""
    from app.routers import health as health_module
    from app.storage import StorageClientError

    class FakeSettings:
        app_name = "LiveReview"
        app_version = "0.5.1"
        app_env = "development"
        storage_configured = True
        storage_missing_fields: list[str] = []

    def boom(_settings):
        raise StorageClientError("未安装 TOS SDK")

    monkeypatch.setattr(health_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(health_module, "build_storage", boom)

    data = client.get("/health").json()
    assert data["storage"] == "unavailable"
    assert data["status"] == "degraded"


def test_health_reports_database_degraded(monkeypatch):
    from app.config import Settings
    from app.routers import health

    for name in ("TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_ENDPOINT", "TOS_REGION", "TOS_BUCKET"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(health, "check_database_ok", lambda: False)
    monkeypatch.setattr(health, "get_settings", lambda: Settings(_env_file=None))

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"] == "degraded"
    # 未配置存储不影响整体状态
    assert data["storage"] == "ok"
