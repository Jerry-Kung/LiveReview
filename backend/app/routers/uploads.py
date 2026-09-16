"""上传接口：创建会话、接收分片、合并并落对象存储。"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import chunks as chunk_store
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
from app.storage import OBJECT_KIND_ORIGINAL, StorageError, describe_error, get_storage
from app.tasks import STATUS_FAILED, STATUS_UPLOADED, STATUS_UPLOADING, submit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# 会话状态取值：uploading 收分片、assembling 合并入库中、completed 已完成、failed 失败可续传
STATUS_UPLOADING_SESSION = "uploading"
STATUS_ASSEMBLING = "assembling"
STATUS_COMPLETED = "completed"
STATUS_FAILED_SESSION = "failed"
ACCEPTING_STATUSES = (STATUS_UPLOADING_SESSION, STATUS_FAILED_SESSION)


def _session_or_404(db: Session, upload_id: str) -> UploadSession:
    session = db.get(UploadSession, upload_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="上传会话不存在")
    return session


def _reopen_if_failed(db: Session, session: UploadSession, task: Task) -> None:
    """失败后重新提交视为继续本次上传：会话退回 uploading 并清掉旧的失败原因。

    分片产物保留在磁盘上，因此重试不需要重传已成功的分片。任务在原始视频真正落存储前
    一律退回 `uploading`，避免「还没入库的任务」被判为可重试。
    """
    if session.status != STATUS_FAILED_SESSION:
        return
    session.status = STATUS_UPLOADING_SESSION
    session.error = None
    session.updated_at = utcnow()
    task.status = STATUS_UPLOADING
    task.error = None
    task.process_token = None
    task.updated_at = utcnow()
    db.commit()


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
        status=STATUS_UPLOADING_SESSION,
    )
    task = Task(
        id=uuid.uuid4().hex[:16],
        kind="ingest",
        status=STATUS_UPLOADING,
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
    _reopen_if_failed(db, session, _upload_task(db, session))

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
    if session.status == STATUS_COMPLETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="本次上传已完成，无需重复提交")
    if session.status == STATUS_ASSEMBLING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="本次上传正在入库，请稍候")

    task = _upload_task(db, session)
    _reopen_if_failed(db, session, task)

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

    session.status = STATUS_ASSEMBLING
    session.updated_at = utcnow()
    db.commit()

    try:
        merged = chunk_store.assemble(settings.media_root, upload_id, total, session.declared_size)
        storage = get_storage()
        object_key = storage.generate_object_key(OBJECT_KIND_ORIGINAL, session.filename)
        stored = storage.upload_file(merged, object_key)
    except (chunk_store.UploadError, StorageError) as exc:
        _mark_failed(db, session, task, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"上传失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 —— 未知异常同样需要落库，避免会话卡在 assembling
        logger.exception("上传会话 %s 合并或入库失败", upload_id)
        _mark_failed(db, session, task, exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"上传失败：{exc}") from exc
    finally:
        # 失败时保留分片：用户重新提交时无需再传一遍 2GB 原文件
        chunk_store.cleanup_merged(settings.media_root, upload_id)

    # 对象已在存储中就位，本地分片完成使命
    chunk_store.cleanup(settings.media_root, upload_id)

    session.status = STATUS_COMPLETED
    session.error = None
    session.updated_at = utcnow()
    task.object_key = stored.object_key
    task.size = stored.size
    task.status = STATUS_UPLOADED
    task.progress = 0
    task.error = None
    # 上次失败可能已写入 token，重新入队前必须清掉，否则认领条件不成立
    task.process_token = None
    task.updated_at = utcnow()
    db.commit()

    submit(task.id)
    logger.info("上传会话 %s 完成：%s（%d 字节）", upload_id, stored.object_key, stored.size)
    return UploadCompleteResponse(
        upload_id=upload_id,
        task_id=task.id,
        object_key=stored.object_key,
        size=stored.size,
        status=session.status,
    )


def _mark_failed(db: Session, session: UploadSession, task: Task, exc: Exception) -> None:
    """把失败原因同时写入上传会话与任务，保证两个入口都能看到原因。"""
    message = f"{type(exc).__name__}: {describe_error(exc)}"
    session.status = STATUS_FAILED_SESSION
    session.error = message
    session.updated_at = utcnow()
    # 对象已落存储时任务不该被判死：原始视频仍可用，重试接口可重新执行
    task.status = STATUS_UPLOADED if task.object_key else STATUS_FAILED
    task.error = message
    task.updated_at = utcnow()
    db.commit()
    logger.error("上传会话 %s 失败：%s", session.id, message)
