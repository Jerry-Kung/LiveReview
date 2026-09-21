"""媒体探测命令行工具：在测试环境后端容器内直接核对某个文件、某个任务或某个任务的切片。

用法：
    python -m app.media.cli /app/media/originals/abc123.ts   # 按文件路径探测
    python -m app.media.cli abc123                           # 按任务 id 前缀探测其本地副本
    python -m app.media.cli abc123 --clips                   # 列出该任务的切片与其时间范围

用途是定位问题：任务报了「探测失败」时，用同一份文件手动跑一遍，能区分「工具/环境问题」
与「媒体文件本身的问题」；探测成功时用于抽查元数据与实际媒体是否一致。`--clips` 用于核对
V0.1.5 的切分结论（片段数、时间区间、体积与覆盖关系）。本命令只读文件、只打印，不写库、
不改任务状态。
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
    parser.add_argument(
        "--clips",
        action="store_true",
        help="列出该任务的切片与时间范围（核对 V0.1.5 的切分结论）",
    )
    args = parser.parse_args(argv)

    from app.config import get_settings

    settings = get_settings()
    target = Path(args.target)
    if target.is_file():
        return _probe_and_report(target, settings, label="按文件路径")
    return _report_task(args.target, settings, with_clips=args.clips)


def _report_task(task_id: str, settings, *, with_clips: bool) -> int:
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

        if with_clips:
            return _report_clips(task, settings)

        # 本地副本路径依赖原始文件后缀，因此需要回查上传会话
        return _probe_and_report(
            original_path(settings.media_root, task.id, filename),
            settings,
            label=f"任务 {task.id}（状态 {task.status}）",
        )
    finally:
        db.close()


def _report_clips(task, settings) -> int:
    """打印切分结论：片段数、时间区间、体积与覆盖关系，供验收抽查。"""
    from app.media.coverage import check_coverage, issues_from_json

    clips = list(task.clips)
    print(f"任务 {task.id}（状态 {task.status}，进度 {task.progress}）")
    print(
        f"切分输入：{task.split_source_format or '未知容器'}，"
        f"时长 {task.split_source_duration_seconds if task.split_source_duration_seconds is not None else '未知'} 秒"
    )
    if task.error:
        print(f"失败原因：{task.error}")
    if not clips:
        print("没有切片记录：任务可能尚未进入切分阶段。")
        return 1

    print(f"\n切片共 {len(clips)} 片（按序号 = 原视频时间顺序）：")
    for clip in clips:
        size = f"{clip.size_bytes} 字节" if clip.size_bytes is not None else "体积未知"
        duration = (
            f"{clip.duration_seconds:.1f}s" if clip.duration_seconds is not None else "时长未知"
        )
        print(
            f"  #{clip.index}  {clip.start_seconds:.1f}s ~ {clip.end_seconds:.1f}s  "
            f"{duration}  {size}  状态 {clip.status}  {clip.object_key or '未上传'}"
        )

    recorded = issues_from_json(task.coverage_issues_json)
    if recorded:
        print("\n覆盖校验问题（入库记录）：")
        for issue in recorded:
            print(f"  [{issue.get('code')}] {issue.get('message')}")
    elif task.split_checked_at is not None:
        print("\n覆盖校验：通过（无问题记录）")
    else:
        print("\n覆盖校验：尚未执行")

    live = check_coverage(
        task.split_source_duration_seconds,
        [(clip.start_seconds, clip.end_seconds) for clip in clips],
    )
    print(f"按当前切片重新校验：{'通过' if not live else '发现 %d 处问题' % len(live)}")
    for issue in live:
        print(f"  [{issue.code}] {issue.message}")
    return 0 if not live else 1


if __name__ == "__main__":
    sys.exit(main())
