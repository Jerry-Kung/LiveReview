"""应用配置：统一从环境变量读取，凭据不落代码不落仓库。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.storage.base import StorageConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "LiveReview"
    app_version: str = "0.1.3.1"
    app_env: str = "development"
    database_url: str = "sqlite:///./data/liverreview.db"
    log_level: str = "INFO"

    # 服务监听地址与端口（端口可在 .env 中通过 APP_PORT 自行配置）
    app_host: str = "127.0.0.1"
    app_port: int = 12439
    app_reload: bool = False

    # 本地产物根目录：上传分片与合并临时文件（容器内为 /app/media，挂命名卷）
    media_root: str = "./media"
    # 上传分片大小（字节）：由后端下发给前端，前端不硬编码
    upload_chunk_size: int = 8 * 1024 * 1024

    # 对象存储：auto（有凭据用 TOS，无凭据回落 Mock）/ tos（强制真实，测试环境使用）/ mock
    storage_backend: str = "auto"

    # TOS 对象存储凭据（无默认值，只从环境变量读取）
    tos_access_key: str | None = None
    tos_secret_key: str | None = None
    tos_endpoint: str | None = None
    tos_region: str | None = None
    tos_bucket: str | None = None
    tos_object_prefix: str = "liverreview/"
    tos_presigned_ttl_seconds: int = 3600

    # 媒体工具路径（V0.1.4 起使用，此处仅预留）
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    @property
    def storage_missing_fields(self) -> list[str]:
        """列出缺失的存储必填项名称；只返回名称，不回显任何取值。"""
        required = {
            "TOS_ACCESS_KEY": self.tos_access_key,
            "TOS_SECRET_KEY": self.tos_secret_key,
            "TOS_ENDPOINT": self.tos_endpoint,
            "TOS_REGION": self.tos_region,
            "TOS_BUCKET": self.tos_bucket,
        }
        return [name for name, value in required.items() if not (value or "").strip()]

    @property
    def storage_configured(self) -> bool:
        return not self.storage_missing_fields

    def storage_config(self) -> StorageConfig:
        """转换为存储模块的配置对象（调用方需先确认 storage_configured）。"""
        return StorageConfig(
            endpoint=self.tos_endpoint or "",
            region=self.tos_region or "",
            bucket=self.tos_bucket or "",
            access_key=self.tos_access_key or "",
            secret_key=self.tos_secret_key or "",
            object_prefix=self.tos_object_prefix,
            presigned_ttl_seconds=self.tos_presigned_ttl_seconds,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
