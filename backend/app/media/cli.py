"""媒体探测命令行工具：在测试环境后端容器内直接核对某个文件或某个任务的探测结果。

用法：
    python -m app.media.cli /app/media/originals/abc123.ts   # 按文件路径探测
    python -m app.media.cli abc123                           # 按任务 id 前缀探测其本地副本

用途是定位问题：任务报了「探测失败」时，用同一份文件手动跑一遍，能区分「工具/环境问题」
与「媒体文件本身的问题」；探测成功时用于抽查元数据与实际媒体是否一致。
本命令只读文件、只打印，不写库、不改任务状态。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from app.media.paths import original_path
from app.media.probe import ProbeError, probe_file


def _probe_and_report(source: Path, settings, *, label: str) -> int:
    print(f"探测对象：{label}")
    print(f"文件路径：{source}")
    if not source.is_file():
        print("文件不存在：本地副本可能已在探测成功后清理，或该任务尚未执行过探测。")
        return 1

    print(f"文件大小：{source.stat().st_size} 字节")
    print(f"ffprobe：{settings.ffprobe_path}（超时 {settings.probe_timeout_seconds} 秒）")

    try:
        metadata = probe_file(
            source,
            ffprobe_path=settings.ffprobe_path,
            timeout_seconds=settings.probe_timeout_seconds,
            max_output_bytes=settings.probe_max_output_bytes,
        )
    except ProbeError as exc:
        print(f"\n探测失败：{exc}")
        return 1

    print(f"\n摘要：{metadata.summary()}")
    for stream in metadata.streams:
        print(json.dumps(stream.to_dict(), ensure_ascii=False))

    print("\n结论：探测成功")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ffprobe 媒体探测（测试环境核对用）")
    parser.add_argument("target", help="本地媒体文件路径，或任务 id（可用前缀）")
    args = parser.parse_args(argv)

    from app.config import get_settings

    settings = get_settings()
    target = Path(args.target)
    if target.is_file():
        return _probe_and_report(target, settings, label="按文件路径")
    return _probe_task(args.target, settings)


def _probe_task(task_id: str, settings) -> int:
    from app.database import SessionLocal
    from app.models import UploadSession
    from app.tasks.runner import list_tasks

    db = SessionLocal()
    try:
        tasks = [task for task in list_tasks(db, limit=50) if task.id.startswith(task_id)]
        if not tasks:
            print(f"未找到匹配的任务 id：{task_id}")
            return 1
        if len(tasks) > 1:
            print(f"任务 id 前缀匹配到多条，请提供更完整的 id：{[t.id for t in tasks]}")
            return 1

        task = tasks[0]
        filename = None
        if task.upload_id:
            upload = db.get(UploadSession, task.upload_id)
            filename = upload.filename if upload else None
        # 本地副本路径依赖原始文件后缀，因此需要回查上传会话
        return _probe_and_report(
            original_path(settings.media_root, task.id, filename),
            settings,
            label=f"任务 {task.id}（状态 {task.status}）",
        )
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
