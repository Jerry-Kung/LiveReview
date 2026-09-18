import os

from app.config import Settings, get_settings


def test_defaults_apply_without_env():
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./data/liverreview.db"
    assert settings.app_env == "development"
    assert settings.app_name == "LiveReview"
    assert settings.log_level == "INFO"
    assert settings.app_port == 12439


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/test.db")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_PORT", "19999")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:////tmp/test.db"
    assert settings.app_env == "test"
    assert settings.app_port == 19999


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


def test_storage_defaults():
    settings = Settings(_env_file=None)
    assert settings.storage_backend == "auto"
    assert settings.tos_object_prefix == "liverreview/"
    assert settings.tos_presigned_ttl_seconds == 3600
    assert settings.storage_configured is False
    assert set(settings.storage_missing_fields) == {
        "TOS_ACCESS_KEY",
        "TOS_SECRET_KEY",
        "TOS_ENDPOINT",
        "TOS_REGION",
        "TOS_BUCKET",
    }


def test_storage_configured_when_all_credentials_present(monkeypatch):
    for name, value in {
        "TOS_ACCESS_KEY": "ak",
        "TOS_SECRET_KEY": "sk",
        "TOS_ENDPOINT": "tos-cn-beijing.volces.com",
        "TOS_REGION": "cn-beijing",
        "TOS_BUCKET": "bu-liverreview",
    }.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert settings.storage_configured is True
    assert settings.storage_missing_fields == []


def test_storage_missing_fields_lists_only_absent_items(monkeypatch):
    monkeypatch.setenv("TOS_ACCESS_KEY", "ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "sk")
    settings = Settings(_env_file=None)
    assert set(settings.storage_missing_fields) == {"TOS_ENDPOINT", "TOS_REGION", "TOS_BUCKET"}


def test_storage_config_maps_settings(monkeypatch):
    monkeypatch.setenv("TOS_ACCESS_KEY", "ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "sk")
    monkeypatch.setenv("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
    monkeypatch.setenv("TOS_REGION", "cn-beijing")
    monkeypatch.setenv("TOS_BUCKET", "bu-liverreview")
    monkeypatch.setenv("TOS_OBJECT_PREFIX", "custom/")
    monkeypatch.setenv("TOS_PRESIGNED_TTL_SECONDS", "600")
    config = Settings(_env_file=None).storage_config()
    assert config.bucket == "bu-liverreview"
    assert config.object_prefix == "custom/"
    assert config.presigned_ttl_seconds == 600
    assert config.access_key == "ak"


def test_app_version_matches_current_release():
    assert Settings(_env_file=None).app_version == "0.3.0"


def test_upload_and_media_defaults():
    settings = Settings(_env_file=None)
    assert settings.media_root == "./media"
    assert settings.upload_chunk_size == 8 * 1024 * 1024


def test_probe_defaults():
    settings = Settings(_env_file=None)
    assert settings.ffprobe_path == "ffprobe"
    assert settings.probe_timeout_seconds == 300
    assert settings.probe_max_output_bytes == 8 * 1024 * 1024


def test_split_defaults():
    """切分约束默认值即验收要求：单片 ≤1GiB 且 ≤60 分钟。"""
    settings = Settings(_env_file=None)
    assert settings.split_max_duration_seconds == 3600
    assert settings.split_max_clip_bytes == 1024 * 1024 * 1024
    assert settings.ffmpeg_path == "ffmpeg"


def test_split_settings_come_from_env(monkeypatch):
    monkeypatch.setenv("SPLIT_MAX_DURATION_SECONDS", "600")
    monkeypatch.setenv("SPLIT_MAX_CLIP_BYTES", "104857600")
    monkeypatch.setenv("SPLIT_MIN_CLIP_SECONDS", "2.5")
    settings = Settings(_env_file=None)
    assert settings.split_max_duration_seconds == 600
    assert settings.split_max_clip_bytes == 104857600
    assert settings.split_min_clip_seconds == 2.5


def test_probe_env_overrides(monkeypatch):
    monkeypatch.setenv("FFPROBE_PATH", "/usr/bin/ffprobe")
    monkeypatch.setenv("PROBE_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("PROBE_MAX_OUTPUT_BYTES", "1024")
    settings = Settings(_env_file=None)
    assert settings.ffprobe_path == "/usr/bin/ffprobe"
    assert settings.probe_timeout_seconds == 30
    assert settings.probe_max_output_bytes == 1024
