"""接口的请求与响应模型：对外契约集中在此，避免路由层散落字典拼装。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UploadCreateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)


class UploadCreateResponse(BaseModel):
    upload_id: str
    task_id: str
    filename: str
    size: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    status: str


class UploadStatusResponse(BaseModel):
    upload_id: str
    task_id: str
    filename: str
    size: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    received_bytes: int
    status: str
    error: str | None = None


class UploadCompleteResponse(BaseModel):
    upload_id: str
    task_id: str
    object_key: str
    size: int
    status: str


class MissingChunksDetail(BaseModel):
    """缺片响应体：前端据此补齐缺失分片后重新提交完成请求。"""

    message: str
    missing_chunks: list[int]
    received_chunks: list[int]
    total_chunks: int


class TaskMetadataResponse(BaseModel):
    """媒体探测结果（V0.1.4）：任务未探测成功时整个对象为 null。

    字段取值遵循「拿不到就是 null」：探测结果缺字段时不填 0 或空串，避免消费方把未知
    当成已知。`probed_at` 是判断「是否探测过」的唯一依据。
    """

    format_name: str | None = None
    duration_seconds: float | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    stream_count: int | None = None
    bit_rate: int | None = None
    content_hash: str | None = None
    probed_at: datetime


class TaskClipResponse(BaseModel):
    """一个切片（V0.1.5）：序号即其在原视频时间轴上的顺序。

    `start_seconds` / `end_seconds` 是片段在切分输入（转封装产物或原始 MP4）中的定位，
    也是 V0.1.6 回看定位的依据。`download_url` 只在片段已落对象存储时给出预签名地址。
    """

    index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float | None = None
    size_bytes: int | None = None
    status: str
    error: str | None = None
    object_key: str | None = None
    download_url: str | None = None


class CoverageIssueResponse(BaseModel):
    """一条整场覆盖校验问题：`code` 供程序判断，`message` 供展示。"""

    code: str
    message: str


class TaskCoverageResponse(BaseModel):
    """切分与覆盖校验结论：`checked_at` 非空表示已校验过。

    未校验时整个对象为 null，避免把「尚未切分」渲染成「校验通过」。
    """

    clip_count: int
    checked_at: datetime
    issues: list[CoverageIssueResponse] = Field(default_factory=list)
    source_format: str | None = None
    source_duration_seconds: float | None = None


class TaskResponse(BaseModel):
    id: str
    kind: str
    status: str
    progress: int
    filename: str | None = None
    object_key: str | None = None
    size: int | None = None
    error: str | None = None
    metadata: TaskMetadataResponse | None = None
    coverage: TaskCoverageResponse | None = None
    clips: list[TaskClipResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
