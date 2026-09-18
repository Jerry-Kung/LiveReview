"""上传接口：创建会话、接收分片、合并并落对象存储。

分片的接收是浏览器驱动的（前端逐片 PUT），但**收齐之后的合并与入库不依赖浏览器**：
`app/uploads.py` 的 `finalize` 由本模块的完成接口与启动恢复共用，因此关掉页面甚至上传
尚未收齐时中断，服务端都能在下次启动时接着把这次上传做完。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import chunks as chunk_store
from app import uploads as pipeline
from app.config import Settings, get_settings
from app.database import get_db
from app.models import Task, UploadSession, utcnow
from app.schemas import (
    MissingChunksDetail,
    UploadCompleteResponse,
    UploadCreateRequest,
    UploadCreateResponse,
    UploadStatusResponse,
)
from app.storage import StorageError
# 上传会话状态与任务状态同名不同义（会话的 `uploading` 指「还在收分片」），因此带前缀导入
from app.tasks import STATUS_UPLOADING as TASK_UPLOADING
from app.tasks import submit
from app.uploads import (
    ACCEPTING_STATUSES,
    SESSION_ASSEMBLING,
    SESSION_COMPLETED,
    SESSION_UPLOADING,
    UploadFinalizeError,
    finalize,
    reopen_if_failed,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _session_or_404(db: Session, upload_id: str) -> UploadSession:
    session = db.get(UploadSession, upload_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="上传会话不存在")
    return session


def _upload_task(db: Session, session: UploadSession) -> Task:
    task = db.query(Task).filter(Task.upload_id == session.id).one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="上传会话缺少关联任务")
    return task


def _build_status(media_root: str, session: UploadSession) -> UploadStatusResponse:
    total = chunk_store.chunk_count(session.declared_size, session.chunk_size)
    received = chunk_store.received_indices(media_root, session.id)
    return UploadStatusResponse(
        upload_id=session.id,
        task_id=session.task.id if session.task else "",
        filename=session.filename,
        size=session.declared_size,
        chunk_size=session.chunk_size,
        total_chunks=total,
        received_chunks=received,
        received_bytes=chunk_store.received_bytes(media_root, session.id),
        status=session.status,
        error=session.error,
    )


@router.post("", response_model=UploadCreateResponse, status_code=status.HTTP_201_CREATED)
def create_upload(
    payload: UploadCreateRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadCreateResponse:
    """创建上传会话；同时建立处于 `uploading` 状态的任务，使进度可被轮询。"""
    upload_id = uuid.uuid4().hex[:16]
    chunk_size = settings.upload_chunk_size
    total = chunk_store.chunk_count(payload.size, chunk_size)

    session = UploadSession(
        id=upload_id,
        filename=payload.filename,
        declared_size=payload.size,
        chunk_size=chunk_size,
        status=SESSION_UPLOADING,
    )
    task = Task(
        id=uuid.uuid4().hex[:16],
        kind="ingest",
        status=TASK_UPLOADING,
        progress=0,
        size=payload.size,
        upload_id=upload_id,
    )
    db.add(session)
    db.add(task)
    db.commit()

    logger.info("创建上传会话 %s：%s（%d 字节，共 %d 片）", upload_id, payload.filename, payload.size, total)
    return UploadCreateResponse(
        upload_id=upload_id,
        task_id=task.id,
        filename=session.filename,
        size=session.declared_size,
        chunk_size=chunk_size,
        total_chunks=total,
        received_chunks=[],
        status=session.status,
    )


@router.put("/{upload_id}/chunks/{index}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_chunk(
    upload_id: str,
    index: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    """接收单个分片（原始字节体）；同序号重传即覆盖，便于失败后补齐。"""
    session = _session_or_404(db, upload_id)
    if session.status not in ACCEPTING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"上传会话当前状态为 {session.status}，不再接收分片",
        )
    reopen_if_failed(db, session, _upload_task(db, session))

    total = chunk_store.chunk_count(session.declared_size, session.chunk_size)
    if index < 0 or index >= total:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"分片序号超出范围：{index}，有效范围 0～{total - 1}",
        )

    data = await request.body()
    expected = chunk_store.expected_chunk_size(session.declared_size, session.chunk_size, index)
    # 大小必须与期望值一致：既拦截异常请求写满磁盘，也拦截被截断的分片
    if len(data) != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"分片 {index} 大小异常：收到 {len(data)} 字节，期望 {expected} 字节",
        )

    chunk_store.save_chunk(settings.media_root, upload_id, index, data)

    received = chunk_store.received_indices(settings.media_root, upload_id)
    session.received_chunks = len(received)
    session.received_bytes = chunk_store.received_bytes(settings.media_root, upload_id)
    session.updated_at = utcnow()
    db.commit()


@router.get("/{upload_id}", response_model=UploadStatusResponse)
def upload_status(
    upload_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadStatusResponse:
    """查询已收分片，供前端在同一次上传中断后跳过已完成分片继续补齐。"""
    session = _session_or_404(db, upload_id)
    return _build_status(settings.media_root, session)


@router.post("/{upload_id}/complete", response_model=UploadCompleteResponse)
def complete_upload(
    upload_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadCompleteResponse:
    """校验分片齐全 → 合并 → 上传对象存储 → 任务转入待处理并交给后台执行器。"""
    session = _session_or_404(db, upload_id)
    if session.status == SESSION_COMPLETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="本次上传已完成，无需重复提交")
    if session.status == SESSION_ASSEMBLING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="本次上传正在入库，请稍候")

    task = _upload_task(db, session)
    reopen_if_failed(db, session, task)

    total = chunk_store.chunk_count(session.declared_size, session.chunk_size)
    missing = chunk_store.missing_indices(settings.media_root, upload_id, total)
    if missing:
        received = chunk_store.received_indices(settings.media_root, upload_id)
        # 缺片不算错误状态：前端按 missing_chunks 补齐后重新提交即可
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MissingChunksDetail(
                message=f"还有 {len(missing)} 个分片未上传",
                missing_chunks=missing,
                received_chunks=received,
                total_chunks=total,
            ).model_dump(),
        )

    session.status = SESSION_ASSEMBLING
    session.updated_at = utcnow()
    db.commit()

    try:
        object_key = finalize(db, session, task, settings)
    except UploadFinalizeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"上传失败：{exc}") from exc
    except StorageError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"上传失败：{exc}") from exc

    submit(task.id)
    logger.info("上传会话 %s 完成：%s（%d 字节）", upload_id, object_key, session.declared_size)
    return UploadCompleteResponse(
        upload_id=upload_id,
        task_id=task.id,
        object_key=object_key,
        size=task.size or session.declared_size,
        status=session.status,
    )
