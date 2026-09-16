"""上传分片的落盘与合并：本模块只处理本地文件，不感知 HTTP 与数据库。

分片目录约定 `{media_root}/chunks/{上传会话 id}/{序号:06d}.part`，序号定长便于排序与缺片检测。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

CHUNK_SUFFIX = ".part"


class UploadError(Exception):
    """上传过程中的可预期失败，路由层据此返回明确原因。"""


def chunks_dir(media_root: str | Path, upload_id: str) -> Path:
    return Path(media_root) / "chunks" / upload_id


def chunk_path(media_root: str | Path, upload_id: str, index: int) -> Path:
    return chunks_dir(media_root, upload_id) / f"{index:06d}{CHUNK_SUFFIX}"


def merged_path(media_root: str | Path, upload_id: str) -> Path:
    """合并临时文件路径（与分片目录同级，位于 media_root 下）。"""
    return Path(media_root) / "assembling" / f"{upload_id}.part"


def expected_chunk_size(declared_size: int, chunk_size: int, index: int) -> int:
    """第 index 片应有的字节数：末片可能不足一整片。"""
    start = index * chunk_size
    return min(chunk_size, declared_size - start)


def chunk_count(declared_size: int, chunk_size: int) -> int:
    return max(1, -(-declared_size // chunk_size))


def received_indices(media_root: str | Path, upload_id: str) -> list[int]:
    """已落盘的分片序号（按升序）。零字节的分片视为未接收。"""
    directory = chunks_dir(media_root, upload_id)
    if not directory.is_dir():
        return []
    indices: list[int] = []
    for path in directory.glob(f"*{CHUNK_SUFFIX}"):
        try:
            index = int(path.stem)
        except ValueError:
            continue
        if path.stat().st_size > 0:
            indices.append(index)
    return sorted(indices)


def missing_indices(media_root: str | Path, upload_id: str, total: int) -> list[int]:
    present = set(received_indices(media_root, upload_id))
    return [index for index in range(total) if index not in present]


def received_bytes(media_root: str | Path, upload_id: str) -> int:
    directory = chunks_dir(media_root, upload_id)
    if not directory.is_dir():
        return 0
    return sum(path.stat().st_size for path in directory.glob(f"*{CHUNK_SUFFIX}"))


def save_chunk(media_root: str | Path, upload_id: str, index: int, data: bytes) -> int:
    """写入单个分片（同序号重传即覆盖），返回该分片字节数。"""
    directory = chunks_dir(media_root, upload_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = chunk_path(media_root, upload_id, index)
    # 先写临时文件再改名，避免中断留下半截分片被误判为已完成
    tmp = path.with_suffix(f"{CHUNK_SUFFIX}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path.stat().st_size


def assemble(media_root: str | Path, upload_id: str, total: int, declared_size: int) -> Path:
    """按序号顺序拼接分片为单个文件，并校验总大小。

    调用方需先确认无缺片；大小不符说明分片被截断或声明有误，抛出 UploadError。
    """
    target = merged_path(media_root, upload_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as out:
        for index in range(total):
            path = chunk_path(media_root, upload_id, index)
            if not path.is_file():
                raise UploadError(f"分片 {index} 缺失，无法合并")
            with path.open("rb") as src:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
                    written += len(block)

    if written != declared_size:
        raise UploadError(
            f"合并后大小 {written} 字节与声明的 {declared_size} 字节不一致，请重新上传"
        )
    return target


def cleanup_merged(media_root: str | Path, upload_id: str) -> None:
    """删除合并临时文件（分片保留）。合并失败后的清理只做这一层。"""
    _remove(merged_path(media_root, upload_id))


def cleanup(media_root: str | Path, upload_id: str) -> None:
    """删除分片目录与合并临时文件：对象已在存储中就位后才可调用。"""
    _remove(chunks_dir(media_root, upload_id))
    _remove(merged_path(media_root, upload_id))


def _remove(path: Path) -> None:
    """清理失败只记 warning，不影响任务结论。"""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError as exc:  # noqa: BLE001 —— 清理是尽力而为，不对外抛错
        logger.warning("上传临时产物清理失败：%s（%s）", path, exc)
