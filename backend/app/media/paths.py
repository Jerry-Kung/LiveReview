"""本地产物路径与内容指纹：探测输入（原始视频副本）的落点在此统一约定。

路径规则集中在一处，避免上传路由、任务体与删除链路各自拼装目录名。
约定：`{media_root}/originals/{任务 id}{后缀}`，后缀取自原始文件名，便于在容器内
直接识别文件类型（ffprobe 也依赖扩展名之外的容器内容，但人工排查时后缀更直观）。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

ORIGINALS_DIRNAME = "originals"


def originals_dir(media_root: str | Path) -> Path:
    return Path(media_root) / ORIGINALS_DIRNAME


def original_path(media_root: str | Path, task_id: str, filename: str | None = None) -> Path:
    """按任务 id 与原始文件名推导本地副本路径（后缀保留，主体不保留）。"""
    suffix = Path(filename).suffix if filename else ""
    return originals_dir(media_root) / f"{task_id}{suffix}"


def remove_original(media_root: str | Path, task_id: str, filename: str | None = None) -> None:
    """删除本地副本；清理是尽力而为，失败只记 warning，不影响任务结论。"""
    path = original_path(media_root, task_id, filename)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:  # noqa: BLE001 —— 清理失败不应让任务判死
        logger.warning("本地原始视频副本清理失败：%s（%s）", path, exc)


def remove_originals_dir(media_root: str | Path) -> None:
    """整目录清理，供测试与人工重置使用。"""
    try:
        shutil.rmtree(originals_dir(media_root), ignore_errors=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("本地原始视频目录清理失败（%s）", exc)


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
