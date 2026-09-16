"""业务数据模型：上传会话与处理任务。

本模块只声明表结构，不含业务规则；状态流转的合法取值集中在 `app/upload.py`
与 `app/tasks/` 中，避免常量散落。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
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
