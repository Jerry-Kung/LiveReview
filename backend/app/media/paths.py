"""本地产物路径与内容指纹：媒体处理各阶段的落点在此统一约定。

路径规则集中在一处，避免上传路由、任务体、切分模块与删除链路各自拼装目录名。
约定：

- `{media_root}/originals/{任务 id}{后缀}`：原始视频副本，探测与切分的输入。
- `{media_root}/converted/{任务 id}.mp4`：转封装产物，供切片使用（V0.1.5）。
- `{media_root}/clips/{任务 id}/clip_{序号:03d}.mp4`：切片产物，上传成功后即删除（V0.1.5）。

序号固定三位并补零，使目录列表的字典序与时间顺序一致，人工排查时不会把第 10 片排在第 2 片前。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

ORIGINALS_DIRNAME = "originals"
CONVERTED_DIRNAME = "converted"
CLIPS_DIRNAME = "clips"

# 片段序号位数：补零后目录列表的字典序等于时间顺序
CLIP_INDEX_WIDTH = 3


def originals_dir(media_root: str | Path) -> Path:
    return Path(media_root) / ORIGINALS_DIRNAME


def original_path(media_root: str | Path, task_id: str, filename: str | None = None) -> Path:
    """按任务 id 与原始文件名推导本地副本路径（后缀保留，主体不保留）。"""
    suffix = Path(filename).suffix if filename else ""
    return originals_dir(media_root) / f"{task_id}{suffix}"


def converted_dir(media_root: str | Path) -> Path:
    return Path(media_root) / CONVERTED_DIRNAME


def converted_path(media_root: str | Path, task_id: str) -> Path:
    """转封装产物路径：统一 MP4 后缀，与原始文件后缀无关。"""
    return converted_dir(media_root) / f"{task_id}.mp4"


def clips_dir(media_root: str | Path, task_id: str) -> Path:
    return Path(media_root) / CLIPS_DIRNAME / task_id


def clip_path(media_root: str | Path, task_id: str, index: int) -> Path:
    """切片产物路径：文件名带补零序号，同时作为对象键的文件名主体。"""
    return clips_dir(media_root, task_id) / f"clip_{index:0{CLIP_INDEX_WIDTH}d}.mp4"


def clip_filename(index: int) -> str:
    """片段文件名（不含目录）：既用于本地落盘，也用于生成对象键。"""
    return f"clip_{index:0{CLIP_INDEX_WIDTH}d}.mp4"


def remove_file(path: str | Path, *, description: str = "本地产物") -> None:
    """删除单个本地产物；清理是尽力而为，失败只记 warning，不影响任务结论。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 —— 清理失败不应让任务判死
        logger.warning("%s清理失败：%s（%s）", description, path, exc)


def remove_original(media_root: str | Path, task_id: str, filename: str | None = None) -> None:
    """删除本地副本。"""
    remove_file(original_path(media_root, task_id, filename), description="本地原始视频副本")


def remove_converted(media_root: str | Path, task_id: str) -> None:
    """删除转封装产物。"""
    remove_file(converted_path(media_root, task_id), description="转封装产物")


def remove_clip(media_root: str | Path, task_id: str, index: int) -> None:
    """删除单个切片产物；上传成功后即调用，避免 GB 级片段长期占用磁盘。"""
    remove_file(clip_path(media_root, task_id, index), description=f"切片产物 #{index}")


def remove_clips_tree(media_root: str | Path, task_id: str) -> None:
    """整目录删除任务的切片产物。

    逐个删除文件会漏掉同目录下的其它残留（如失败时写了一半的文件、同名 sidecar），下一轮
    会按相同序号重新切分，残留只会让「这份文件是否有效」的判断变脏。
    """
    try:
        shutil.rmtree(clips_dir(media_root, task_id), ignore_errors=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("切片目录清理失败（任务 %s）：%s", task_id, exc)


def remove_task_media(media_root: str | Path, task_id: str, filename: str | None = None) -> None:
    """按任务 id 清空其全部本地产物：副本、转封装产物、切片与片段目录。"""
    remove_original(media_root, task_id, filename)
    remove_converted(media_root, task_id)
    remove_clips_tree(media_root, task_id)


def sha256_file(path: str | Path, block_size: int = 4 * 1024 * 1024) -> str:
    """流式计算文件 sha256，避免把 GB 级文件读进内存。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
