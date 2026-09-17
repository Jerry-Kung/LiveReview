"""已上传视频的删除：清理对象存储、本地产物与数据库记录。

删除顺序为「对象存储 → 本地产物 → 数据库记录」，任何一步失败都中止并保留记录：
数据库行是唯一能再次定位对象与分片的线索，先删记录会让残留数据永远无法回收。
对象存储的 `delete_object` 契约对「对象不存在」幂等，重复删除不会因缺对象而失败。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app import chunks as chunk_store
from app.media.paths import remove_task_media
from app.models import Task
from app.storage import get_storage

logger = logging.getLogger(__name__)


def _clip_object_keys(db: Session, task: Task) -> list[str]:
    """任务的片段对象键：切片是 V0.1.5 起的正式产物，删除任务时必须一并回收。"""
    return [clip.object_key for clip in task.clips if clip.object_key]


def delete_task_assets(db: Session, task: Task, media_root: str) -> None:
    """删除一个任务的全部产物与记录。

    调用方需先确认任务不处于后台执行中；存储异常（凭证、网络、服务端错误）向上抛出，
    由路由层转换为明确失败原因，此时任务记录仍在，可再次发起删除。
    """
    # 先删云端对象：失败即中止，避免出现「记录已删、视频还留在桶里」的孤儿对象。
    # 顺序为原始视频 → 各切片，任一失败都中止，记录保留以便重试删除。
    if task.object_key:
        get_storage().delete_object(task.object_key)
    for object_key in _clip_object_keys(db, task):
        get_storage().delete_object(object_key)

    # 本地产物按任务 id 命名，与对象存储中的对象一并清理（副本、转封装产物、切片目录）
    remove_task_media(media_root, task.id, task.filename)

    # 分片目录与合并临时文件是尽力而为的清理，不因本地文件异常阻断记录删除
    if task.upload_id:
        chunk_store.cleanup(media_root, task.upload_id)

    from app.tasks.runner import remove_task_records

    remove_task_records(db, task)
    logger.info("已删除任务 %s（对象键 %s，含 %d 个切片）", task.id, task.object_key, len(task.clips))
