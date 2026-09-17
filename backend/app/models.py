"""业务数据模型：上传会话与处理任务。

本模块只声明表结构，不含业务规则；状态流转的合法取值集中在 `app/upload.py`
与 `app/tasks/` 中，避免常量散落。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """统一使用带时区的 UTC 时间，避免 SQLite 与容器时区差异带来的歧义。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# 片段状态：本地是否还有切片产物、对象存储里是否已有对象，都由此判断
CLIP_STATUS_PENDING = "pending"
CLIP_STATUS_UPLOADED = "uploaded"
CLIP_STATUS_FAILED = "failed"


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

    # 预处理与切分结果（V0.1.5）
    # 切分输入可能来自转封装产物而非原始文件，其时长/容器才是时间轴基准，须单独记录
    split_source_format: Mapped[str | None] = mapped_column(String(128), nullable=True)
    split_source_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 覆盖校验结论：JSON 问题列表（空表示通过），split_checked_at 是「是否校验过」的依据
    coverage_issues_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    split_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    upload_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("upload_sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    upload: Mapped[UploadSession | None] = relationship(back_populates="task")
    clips: Mapped[list["MediaClip"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="MediaClip.index",
    )

    @property
    def filename(self) -> str | None:
        return self.upload.filename if self.upload is not None else None

    @property
    def has_metadata(self) -> bool:
        """是否已有探测结果：供接口与前端区分「未探测」与「探测出空值」。"""
        return self.probed_at is not None

    @property
    def has_coverage_check(self) -> bool:
        """是否已做过覆盖校验：区分「校验通过」与「尚未校验」。"""
        return self.split_checked_at is not None


class MediaClip(Base):
    """切片记录：一个任务按时间顺序切出的第 N 片，与其在原视频中的时间范围。

    `index` 是顺序的唯一依据（0 起递增，即切分与上传的先后顺序），不用创建时间排序——
    上传完成的先后受网络波动影响，会与时间轴顺序不一致。

    对象键在首次生成后入库并在重试时复用：重试若换键会让同一片段在桶里留下多份副本。
    """

    __tablename__ = "media_clips"
    __table_args__ = (UniqueConstraint("task_id", "index", name="uq_media_clips_task_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    index: Mapped[int] = mapped_column(Integer)

    # 在切分输入中的时间范围（秒）：即片段在原场直播中的定位，V0.1.6 回看依赖它
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    # 实测值：-c copy 的切点落在关键帧上，实际时长与体积以探测与 stat 为准
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=CLIP_STATUS_PENDING)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="clips")

    @property
    def is_uploaded(self) -> bool:
        """是否已成功落对象存储：重试时据此跳过，不重复上传。"""
        return self.status == CLIP_STATUS_UPLOADED and bool(self.object_key)
