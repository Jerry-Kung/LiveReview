import os

from app.config import Settings, get_settings


def test_defaults_apply_without_env():
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./data/liverreview.db"
    assert settings.app_env == "development"
    assert settings.app_name == "LiveReview"
    assert settings.log_level == "INFO"


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/test.db")
    monkeypatch.setenv("APP_ENV", "test")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:////tmp/test.db"
    assert settings.app_env == "test"


def test_tos_credentials_have_no_default():
    settings = Settings(_env_file=None)
    assert settings.tos_access_key is None
    assert settings.tos_secret_key is None


def test_get_settings_caches_instance(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("APP_ENV", "test")
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()
