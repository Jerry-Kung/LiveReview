from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "LiveReview"
    assert data["version"] == "0.1.2"
    assert data["environment"] == "development"
    assert data["database"] in ("ok", "degraded")


def test_health_reports_database_degraded(monkeypatch):
    def fake_check() -> bool:
        return False

    from app.routers import health

    monkeypatch.setattr(health, "check_database_ok", fake_check)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"] == "degraded"
