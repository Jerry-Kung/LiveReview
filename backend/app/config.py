"""应用配置：统一从环境变量读取，凭据不落代码不落仓库。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "LiveReview"
    app_version: str = "0.1.1"
    app_env: str = "development"
    database_url: str = "sqlite:///./data/liverreview.db"
    log_level: str = "INFO"

    # TOS 对象存储凭据（V0.1.2 起使用，此处仅预留，无默认值）
    tos_access_key: str | None = None
    tos_secret_key: str | None = None
    tos_endpoint: str | None = None
    tos_region: str | None = None
    tos_bucket: str | None = None

    # 媒体工具路径（V0.1.4 起使用，此处仅预留）
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"


@lru_cache
def get_settings() -> Settings:
    return Settings()
