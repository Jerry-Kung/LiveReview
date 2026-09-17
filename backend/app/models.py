"""业务数据模型：上传会话与处理任务。

本模块只声明表结构，不含业务规则；状态流转的合法取值集中在 `app/upload.py`
与 `app/tasks/` 中，避免常量散落。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """统一使用带时区的 UTC 时间，避免 SQLite 与容器时区差异带来的歧义。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UploadSession(Base):
    """一次大文件上传的会话：记录分片进度，落盘位置由 id 推导。"""

    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    declared_size: Mapped[int] = mapped_column(Integer)
    chunk_size: Mapped[int] = mapped_column(Integer)
    received_bytes: Mapped[int] = mapped_column(Integer, default=0)
    received_chunks: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="uploading")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    task: Mapped["Task | None"] = relationship(back_populates="upload", uselist=False)


class Task(Base):
    """处理任务：上传完成后建立，承载后续版本的探测与切分。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="ingest")
    status: Mapped[str] = mapped_column(String(16), default="uploading")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 认领判据：执行器写入 token 后据此判断任务是否已被处理，避免重复执行
    process_token: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 媒体探测结果（V0.1.4）：常用字段升为列，供验收核对与 V0.1.5 切分直接取用；
    # 完整 ffprobe 输出另存 metadata_json，两者同时写入，不依赖消费方解析 JSON。
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    media_format: Mapped[str | None] = mapped_column(String(128), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stream_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_bit_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    upload_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("upload_sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    upload: Mapped[UploadSession | None] = relationship(back_populates="task")

    @property
    def filename(self) -> str | None:
        return self.upload.filename if self.upload is not None else None

    @property
    def has_metadata(self) -> bool:
        """是否已有探测结果：供接口与前端区分「未探测」与「探测出空值」。"""
        return self.probed_at is not None
