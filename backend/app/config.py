"""应用配置：统一从环境变量读取，凭据不落代码不落仓库。"""

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.base import LLMConfig
from app.storage.base import StorageConfig

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "LiveReview"
    app_version: str = "0.5.2"
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

    # 媒体工具路径（V0.1.4 起用 ffprobe 探测，V0.1.5 起用 ffmpeg 转封装与切分）。
    # 两者既可以是单个可执行文件名，也可以是「解释器 + 脚本」形式的列表
    # （列表供测试用假脚本替换真实工具，使未安装 FFmpeg 的机器也能覆盖完整链路）。
    ffmpeg_path: str | list[str] = "ffmpeg"
    ffprobe_path: str | list[str] = "ffprobe"
    # 单次探测超时（秒）：1～2GB 的 TS 配 -count_packets 需要完整读一遍文件，留足余量
    probe_timeout_seconds: int = 300
    # ffprobe 标准输出长度上限（字节）：拦截异常输入导致的输出膨胀
    probe_max_output_bytes: int = 8 * 1024 * 1024

    # 切分约束（V0.1.5）：单片时长与体积上限，均为任务方要求的硬约束，从 .env 可调
    split_max_duration_seconds: int = 3600  # 单片最长 60 分钟
    split_max_clip_bytes: int = 1024 * 1024 * 1024  # 单片最大 1GiB
    # 体积超限时递归对半的下限：到达该长度仍超限说明码率高到无法满足约束，判失败而非无限细分
    split_min_clip_seconds: float = 1.0
    # 单次转封装/切片调用的超时（秒）：GB 级文件的流复制耗时随体积增长
    split_timeout_seconds: int = 600
    # ffmpeg stderr 收集上限（字节）：失败原因要能入库，但不该塞进整段日志
    ffmpeg_max_output_bytes: int = 8 * 1024 * 1024

    # 模型接入（V0.2）：base_url / api_key / model_name 三项必填，只从环境变量读取
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model_name: str | None = None
    # 单次视频理解请求的超时（秒）：几分钟级的任务，默认 900s，可按素材长度上调
    llm_timeout_seconds: int = 900
    # 送模型的采样帧率：1fps 是示例代码的取值，兼顾画面变化与请求体积
    llm_fps: float = 1.0
    # 单片识别的最大尝试次数（含首次）：瞬时故障退避重试，超出即判该片失败
    llm_max_attempts: int = 3
    # 片段识别的并发数：模型侧限流与配额未知，默认串行，确认配额后再上调
    llm_concurrency: int = 1
    # 预签名视频 URL 的有效期（秒）：须覆盖「排队等待 + 单次识别」的耗时
    llm_clip_url_ttl_seconds: int = 7200
    # 识别任务的提示词：留空使用默认题面（识别片段中的所有人声语音并判断语气）
    llm_understanding_prompt: str | None = None

    # 复盘接入（V0.3）：与识别共用 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_NAME
    # 单次复盘请求的超时（秒）：纯文本分析，量级远小于视频理解
    review_timeout_seconds: int = 300
    # 单次复盘的最大尝试次数（含首次）：瞬时故障退避重试
    review_max_attempts: int = 3
    # 单次调用送出的转写正文字符数上限：超出则按片段边界分批，多次调用后合并
    # 中文按「1 汉字 ≈ 1 token」估算，20000 字约 2 万 token，留足输出与系统提示的余量
    review_input_chars_per_call: int = 20000

    # 用户登录（V0.5.1）：账号存在数据库的 `users` 表里，不再从环境变量读取。
    # 建号用 `python -m app.auth.init_admin`（冷启动）或 `--list` 查看已有账号。
    # 会话有效期（秒）：默认 12 小时，每次访问顺延，但总时长不超过下面的绝对上限
    auth_session_ttl_seconds: int = 12 * 3600
    # 会话的绝对有效期（秒）：默认 7 天。滑动续期没有上限就等于一次登录永久有效，
    # 这一项是它的天花板——超过这个时间必须重新登录
    auth_session_absolute_ttl_seconds: int = 7 * 24 * 3600
    # 仅 HTTPS 下发送会话 Cookie：本地开发为 http，须保持关闭，部署到 HTTPS 时打开
    auth_cookie_secure: bool = False

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

    @property
    def llm_missing_fields(self) -> list[str]:
        """列出缺失的模型必填项名称；只返回名称，不回显任何取值。"""
        required = {
            "LLM_BASE_URL": self.llm_base_url,
            "LLM_API_KEY": self.llm_api_key,
            "LLM_MODEL_NAME": self.llm_model_name,
        }
        return [name for name, value in required.items() if not (value or "").strip()]

    @property
    def llm_configured(self) -> bool:
        return not self.llm_missing_fields

    def llm_config(self) -> LLMConfig:
        """转换为模型接入模块的配置对象（调用方需先确认 llm_configured）。"""
        return LLMConfig(
            base_url=self.llm_base_url or "",
            api_key=self.llm_api_key or "",
            model_name=self.llm_model_name or "",
            timeout_seconds=self.llm_timeout_seconds,
            fps=self.llm_fps,
            max_attempts=self.llm_max_attempts,
            review_timeout_seconds=self.review_timeout_seconds,
            review_max_attempts=self.review_max_attempts,
        )

    # ---- 用户登录（V0.5.1）----

    def auth_account_state(self) -> tuple[int, str | None]:
        """账号数量与需要提醒的问题，供启动告警与 `GET /api/auth/status` 共用。

        查库失败时不抛异常：状态查询与启动日志都不该因为数据库暂时不可用而中断，
        这与 `check_database_ok()` 的处理方式一致。返回的 `None` 表示「查不到」，
        与「确实一个账号都没有」是两件事，因此不用 0 表示失败。
        """
        from app.auth import store
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            total = store.count_users(db)
        except Exception:  # noqa: BLE001 —— 告警路径不因数据库异常而中断启动
            logger.exception("读取账号数量失败")
            return 0, "无法读取账号表：请确认数据库可用，并执行 python -m app.auth.init_admin 建号"
        finally:
            db.close()

        if total == 0:
            return 0, "没有任何账号可登录：请执行 python -m app.auth.init_admin 创建管理员"
        return total, None

    @property
    def auth_warnings(self) -> list[str]:
        """启动时的鉴权配置告警；不回显任何取值。"""
        _, problem = self.auth_account_state()
        return [problem] if problem else []


@lru_cache
def get_settings() -> Settings:
    return Settings()
