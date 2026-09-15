from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert data["service"] == "LiveReview"
    assert data["version"] == "0.1.2"
    assert data["environment"] == "development"
    assert data["database"] in ("ok", "degraded")
    assert data["storage"] in ("configured", "not_configured", "unavailable")
    assert isinstance(data["storage_missing"], list)


def test_health_reports_storage_not_configured_without_credentials(monkeypatch):
    """缺凭据时返回 not_configured 且不降级：用受控 Settings，不依赖本机 .env 是否存在。"""
    from app.config import Settings
    from app.routers import health as health_module

    for name in ("TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_ENDPOINT", "TOS_REGION", "TOS_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(health_module, "get_settings", lambda: settings)

    resp = client.get("/health")
    data = resp.json()
    assert data["storage"] == "not_configured"
    assert "TOS_ACCESS_KEY" in data["storage_missing"]
    # 未配置存储不使整体状态降级：V0.1.2 尚不依赖存储
    assert data["status"] in ("ok", "degraded")  # 取决于数据库探针
    assert data["storage"] != "unavailable"


def test_health_reports_storage_configured(monkeypatch):
    """已配置存储时返回 configured：用桩 Settings 避免真实读取 .env。"""
    from app.routers import health as health_module

    class FakeSettings:
        app_name = "LiveReview"
        app_version = "0.1.2"
        app_env = "development"
        storage_configured = True
        storage_missing_fields: list[str] = []

    monkeypatch.setattr(health_module, "get_settings", lambda: FakeSettings())
    # FakeSettings 是精简桩对象，不具备真实 build_storage 所需的 storage_backend 等属性，
    # 此处额外桩掉 build_storage 以隔离"配置齐全→configured"这一条路径。
    monkeypatch.setattr(health_module, "build_storage", lambda _settings: object())
    resp = client.get("/health")
    data = resp.json()
    assert data["storage"] == "configured"
    assert data["storage_missing"] == []
    assert data["status"] in ("ok", "degraded")  # 取决于数据库探针


def test_health_reports_storage_unavailable_when_build_fails(monkeypatch):
    from app.routers import health as health_module
    from app.storage import StorageClientError

    class FakeSettings:
        app_name = "LiveReview"
        app_version = "0.1.2"
        app_env = "development"
        storage_configured = True
        storage_missing_fields: list[str] = []

    def boom(_settings):
        raise StorageClientError("未安装 TOS SDK")

    monkeypatch.setattr(health_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(health_module, "build_storage", boom)
    resp = client.get("/health")
    data = resp.json()
    assert data["storage"] == "unavailable"
    assert data["status"] == "degraded"


def test_health_reports_database_degraded(monkeypatch):
    from app.config import Settings
    from app.routers import health

    def fake_check() -> bool:
        return False

    for name in ("TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_ENDPOINT", "TOS_REGION", "TOS_BUCKET"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(health, "check_database_ok", fake_check)
    monkeypatch.setattr(health, "get_settings", lambda: Settings(_env_file=None))
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"] == "degraded"
    assert data["storage"] == "not_configured"  # 未配置不影响整体状态
