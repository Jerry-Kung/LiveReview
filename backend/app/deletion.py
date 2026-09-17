"""已上传视频的删除：清理对象存储、本地产物与数据库记录。

删除顺序为「对象存储 → 本地产物 → 数据库记录」，任何一步失败都中止并保留记录：
数据库行是唯一能再次定位对象与分片的线索，先删记录会让残留数据永远无法回收。
对象存储的 `delete_object` 契约对「对象不存在」幂等，重复删除不会因缺对象而失败。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app import chunks as chunk_store
from app.media.paths import remove_original
from app.models import Task
from app.storage import get_storage

logger = logging.getLogger(__name__)


def delete_task_assets(db: Session, task: Task, media_root: str) -> None:
    """删除一个任务的全部产物与记录。

    调用方需先确认任务不处于后台执行中；存储异常（凭证、网络、服务端错误）向上抛出，
    由路由层转换为明确失败原因，此时任务记录仍在，可再次发起删除。
    """
    if task.object_key:
        # 先删云端对象：失败即中止，避免出现「记录已删、视频还留在桶里」的孤儿对象
        get_storage().delete_object(task.object_key)

    # 待探测的本地副本按任务 id 命名，与对象存储中的对象一并清理
    remove_original(media_root, task.id, task.filename)

    # 分片目录与合并临时文件是尽力而为的清理，不因本地文件异常阻断记录删除
    if task.upload_id:
        chunk_store.cleanup(media_root, task.upload_id)

    from app.tasks.runner import remove_task_records

    remove_task_records(db, task)
    logger.info("已删除任务 %s（对象键 %s）", task.id, task.object_key)
