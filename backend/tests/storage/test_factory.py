"""工厂测试：三种 storage_backend 取值的选择与异常行为。"""

import importlib.util

import pytest

from app.config import Settings
from app.storage import build_storage, get_storage, reset_storage
from app.storage.base import StorageClientError
from app.storage.mock import InMemoryStorage
from app.storage.tos_backend import TosStorage

TOS_ENV = {
    "TOS_ACCESS_KEY": "ak",
    "TOS_SECRET_KEY": "sk",
    "TOS_ENDPOINT": "tos-cn-beijing.volces.com",
    "TOS_REGION": "cn-beijing",
    "TOS_BUCKET": "bu-liverreview",
}


@pytest.fixture(autouse=True)
def _clear_storage_singleton():
    reset_storage()
    yield
    reset_storage()


def _settings_without_env(monkeypatch, **overrides) -> Settings:
    for key in list(TOS_ENV) + ["STORAGE_BACKEND"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_auto_without_credentials_falls_back_to_mock(monkeypatch):
    settings = _settings_without_env(monkeypatch)
    assert isinstance(build_storage(settings), InMemoryStorage)


def test_auto_with_credentials_uses_tos(monkeypatch):
    settings = _settings_without_env(monkeypatch, **TOS_ENV)
    assert isinstance(build_storage(settings), TosStorage)


def test_mock_backend_forces_mock_even_with_credentials(monkeypatch):
    settings = _settings_without_env(monkeypatch, STORAGE_BACKEND="mock", **TOS_ENV)
    assert isinstance(build_storage(settings), InMemoryStorage)


def test_tos_backend_requires_credentials(monkeypatch):
    settings = _settings_without_env(monkeypatch, STORAGE_BACKEND="tos")
    with pytest.raises(StorageClientError) as excinfo:
        build_storage(settings)
    message = str(excinfo.value)
    assert "TOS_ACCESS_KEY" in message
    assert "TOS_SECRET_KEY" in message


def test_tos_backend_without_sdk_raises_client_error(monkeypatch):
    settings = _settings_without_env(monkeypatch, STORAGE_BACKEND="tos", **TOS_ENV)

    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name, *args, **kwargs):
        if name == "tos":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)

    with pytest.raises(StorageClientError) as excinfo:
        build_storage(settings)
    assert "tos" in str(excinfo.value)


def test_unknown_backend_is_rejected(monkeypatch):
    settings = _settings_without_env(monkeypatch, STORAGE_BACKEND="s3")
    with pytest.raises(StorageClientError):
        build_storage(settings)


def test_get_storage_returns_singleton(monkeypatch):
    _settings_without_env(monkeypatch)
    # 让单例工厂使用受控配置：直接把 get_settings 缓存清掉后读取环境
    from app.config import get_settings

    get_settings.cache_clear()
    assert get_storage() is get_storage()
    get_settings.cache_clear()
