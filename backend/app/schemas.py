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


class TaskResponse(BaseModel):
    id: str
    kind: str
    status: str
    progress: int
    filename: str | None = None
    object_key: str | None = None
    size: int | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
