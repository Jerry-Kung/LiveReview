"""项目数据的两类清理：本地残留视频与对象存储的过期视频。

V0.6.1 引入。此前本地会保留原始副本与转封装产物（失败时长期保留），对象存储里的视频
则永久留存——测试环境试用一段时间后，磁盘与桶都会被 GB 级录屏挤满。本模块负责把这两处
都收干净：

- `sweep_orphan_media`：启动时清掉 `MEDIA_ROOT` 下已无主（任务或上传会话已删）的视频
  文件，兜住任务体释放前进程被杀等中断场景。
- `sweep_expired_videos`：按 `STORAGE_VIDEO_TTL_SECONDS` 删除云端到期的原始视频与切片，
  只清视频、保留转写与复盘结论。由 `app/tasks/executor.py` 的定时线程周期性调用。

两个入口都**只记日志不抛异常**：清理是尽力而为的维护动作，失败不该让服务启动或后台
线程停下来。这一取舍与 `app/media/paths.py` 里 `remove_file` 的处理方式一致。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.media.paths import (
    CLIPS_DIRNAME,
    CONVERTED_DIRNAME,
    ORIGINALS_DIRNAME,
    clips_dir,
    converted_path,
    original_path,
)
from app.models import (
    REVIEW_STATUS_RUNNING,
    UNDERSTANDING_STATUS_RUNNING,
    MediaClip,
    Task,
    UploadSession,
    utcnow,
)
from app.storage import StorageError, describe_error, get_storage
from app.tasks.runner import STATUS_PROCESSING

logger = logging.getLogger(__name__)

# 过期清理写入 `task.error` 的说明：这条错误是用户看到「视频没了」时的第一手解释，
# 必须写清楚发生了什么、以及下一步该怎么办
EXPIRED_REASON = (
    "视频已超过 72 小时保留期并自动清理；转写与复盘结论仍然保留，"
    "如需继续识别请重新上传视频"
)


def _remove(path: Path, description: str) -> bool:
    """删除一个本地路径（文件或目录）；返回是否删除成功，失败只记 warning。"""
    try:
        if path.is_dir():
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
        else:
            return False
    except OSError as exc:  # noqa: BLE001 —— 清理失败不影响调用方
        logger.warning("%s清理失败：%s（%s）", description, path, exc)
        return False
    return True


def sweep_orphan_media(media_root: str | Path, *, db: Session | None = None) -> int:
    """清掉 `MEDIA_ROOT` 下已无主的视频文件，返回清理的条目数。

    「无主」指数据库里已经找不到对应的任务或上传会话——任务被删、或任务体释放前进程被杀。
    有主的一概不动：正在上传的分片、等待处理的副本都还有用，误删会让用户重传 GB 级文件。

    这是启动时的一次性动作（与 `requeue_pending` 同处），不需要常驻定时器。
    """
    from app.database import SessionLocal

    close = db is None
    db = db or SessionLocal()
    try:
        task_ids = {row for row in db.execute(select(Task.id)).scalars()}
        upload_ids = {row for row in db.execute(select(UploadSession.id)).scalars()}
    except Exception:  # noqa: BLE001 —— 清理失败不拦服务启动
        logger.exception("扫描无主本地产物失败")
        return 0
    finally:
        if close:
            db.close()

    root = Path(media_root)
    removed = 0

    # 任务体已释放的音视频产物：`originals/{任务 id}{后缀}`、`converted/{任务 id}.mp4`
    originals = root / ORIGINALS_DIRNAME
    if originals.is_dir():
        for path in originals.iterdir():
            # 文件名主体即任务 id，后缀由原始文件名带出
            if path.is_file() and path.stem not in task_ids:
                removed += _remove(path, "无主原始副本")
    converted = root / CONVERTED_DIRNAME
    if converted.is_dir():
        for path in converted.glob("*.mp4"):
            if path.stem not in task_ids:
                removed += _remove(path, "无主转封装产物")

    # 切片目录：任务记录已不在，整目录回收
    clips_root = root / CLIPS_DIRNAME
    if clips_root.is_dir():
        for path in clips_root.iterdir():
            if path.is_dir() and path.name not in task_ids:
                removed += _remove(path, "无主切片目录")

    # 分片目录与合并临时文件：上传会话已不在（会话与任务一并删除）
    chunks_root = root / "chunks"
    if chunks_root.is_dir():
        for path in chunks_root.iterdir():
            if path.is_dir() and path.name not in upload_ids:
                removed += _remove(path, "无主分片目录")
    assembling = root / "assembling"
    if assembling.is_dir():
        # 合并产物一旦成功就会转存为副本，留在 assembling 下的都是失败残片
        for path in assembling.glob("*.part"):
            removed += _remove(path, "残留合并临时文件")

    if removed:
        logger.info("已清理 %d 项无主本地产物", removed)
    return removed


def sweep_expired_videos(
    *,
    db: Session | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> int:
    """删除对象存储中已过保留期的视频，返回处理的任务数。

    只删视频、不删结论：转写与复盘结论早已落库，与视频是否还在无关。因此过了保留期的
    历史任务仍然可以查看与下载报告，只是不能再启动或重试识别。

    在途任务一律跳过：任务体正在读那个对象，识别正在用预签名地址拉流，此时删除只会
    制造半个世界的状态。跳过不是放弃——下一轮扫描时它大概率已经跑完。
    """
    from app.database import SessionLocal

    settings = settings or get_settings()
    close = db is None
    db = db or SessionLocal()
    now = now or utcnow()
    deadline = now - timedelta(seconds=settings.storage_video_ttl_seconds)

    try:
        candidates = _expired_candidates(db, deadline)
    except Exception:  # noqa: BLE001 —— 查询失败不该让定时线程退出
        logger.exception("扫描过期视频失败")
        if close:
            db.close()
        return 0

    try:
        return _expire_all(db, candidates, now)
    finally:
        if close:
            db.close()


def _expired_candidates(db: Session, deadline: datetime) -> list[Task]:
    """到期且尚未清理的任务：`created_at` 早于截止时刻、视频还在、且没被标过过期。"""
    stmt = (
        select(Task)
        .where(
            Task.object_key.is_not(None),
            Task.video_expired_at.is_(None),
            Task.created_at <= deadline,
            # 在途任务留给下一轮
            Task.status != STATUS_PROCESSING,
            Task.understanding_status != UNDERSTANDING_STATUS_RUNNING,
            Task.review_status != REVIEW_STATUS_RUNNING,
        )
        .order_by(Task.created_at)
    )
    return list(db.execute(stmt).scalars())


def _expire_all(db: Session, tasks: list[Task], now: datetime) -> int:
    """逐个任务删除云端视频；单个失败只记日志，不影响其余任务。"""
    storage = get_storage()
    expired = 0

    for task in tasks:
        keys = [task.object_key] if task.object_key else []
        keys.extend(clip.object_key for clip in _clips_of(db, task.id) if clip.object_key)

        failed = False
        for object_key in keys:
            try:
                storage.delete_object(object_key)
            except StorageError as exc:
                # 保留 object_key 与未标记状态，下一轮重试；否则会留下永远无人回收的孤儿对象
                logger.warning(
                    "任务 %s 的过期对象删除失败，留待下一轮：%s（%s）",
                    task.id,
                    object_key,
                    describe_error(exc),
                )
                failed = True
        if failed:
            continue

        task.object_key = None
        task.video_expired_at = now
        # 过期说明覆盖任务级错误：用户看到的就是「为什么这条任务的视频不见了」
        task.error = EXPIRED_REASON
        task.updated_at = utcnow()
        db.commit()
        expired += 1
        logger.info("任务 %s 的视频已按保留期清理（回收 %d 个对象）", task.id, len(keys))

    return expired


def _clips_of(db: Session, task_id: str) -> list[MediaClip]:
    return list(db.execute(select(MediaClip).where(MediaClip.task_id == task_id)).scalars())


__all__ = [
    "EXPIRED_REASON",
    "sweep_expired_videos",
    "sweep_orphan_media",
]
