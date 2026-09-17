"""ffprobe 子进程调用：探测一个本地媒体文件。

调用约定：以参数列表传参（不经 shell），带超时与输出长度上限；非零退出码、超时、
可执行文件缺失、输出无法解析都映射为 `ProbeError`，由任务体写入任务的失败原因。
本模块不落库、不感知对象存储，只回答「这个文件是什么媒体」。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from app.media.ffprobe import MediaMetadata, ProbeParseError, has_playable_streams, parse_probe_output

logger = logging.getLogger(__name__)

# stderr 摘要长度：失败原因要能入库并展示给用户，但不该把整段 stderr 塞进数据库
STDERR_EXCERPT_LENGTH = 500


class ProbeError(Exception):
    """探测失败：原因可直接作为任务的失败信息展示与入库。"""


def build_ffprobe_args(ffprobe_command: str | Sequence[str], source: str | Path) -> list[str]:
    """构造 ffprobe 参数。

    命令可以是单个可执行文件名，也可以是「解释器 + 脚本」形式的序列（测试环境用假
    ffprobe 脚本时采用后者）。`-count_packets` 让 TS 等容器头无 duration 的录屏也能算出
    时长（V0.1.5 的时间基准依赖该值）；`-v error` 只让真正的错误进入 stderr。
    """
    prefix = [ffprobe_command] if isinstance(ffprobe_command, str) else list(ffprobe_command)
    if not prefix:
        raise ProbeError("ffprobe 命令为空，无法探测")

    return [
        *prefix,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-count_packets",
        str(source),
    ]


def _excerpt(text: str) -> str:
    excerpt = (text or "").strip().replace("\n", " ")
    if len(excerpt) <= STDERR_EXCERPT_LENGTH:
        return excerpt
    return excerpt[:STDERR_EXCERPT_LENGTH] + "…"


def probe_file(
    source: str | Path,
    *,
    ffprobe_path: str | Sequence[str] = "ffprobe",
    timeout_seconds: int = 300,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> MediaMetadata:
    """对本地文件执行 ffprobe 并返回解析后的元数据。

    失败一律抛 `ProbeError`，其字符串即为用户可见的失败原因。
    """
    path = Path(source)
    if not path.is_file():
        raise ProbeError(f"待探测的文件不存在或不是普通文件：{path.name}")

    # 只对「裸可执行文件名」做存在性预检：绝对路径与「解释器 + 脚本」形式由调用方负责
    is_bare_command_name = (
        isinstance(ffprobe_path, str)
        and ffprobe_path != ""
        and os.sep not in ffprobe_path
        and "/" not in ffprobe_path
        and (os.altsep is None or os.altsep not in ffprobe_path)
    )
    if is_bare_command_name and shutil.which(ffprobe_path) is None:
        raise ProbeError(
            f"未找到 ffprobe（{ffprobe_path}）：本地不部署 FFmpeg，探测需在测试环境执行，"
            "或通过 FFPROBE_PATH 指定可执行文件路径"
        )

    args = build_ffprobe_args(ffprobe_path, path)
    try:
        completed = subprocess.run(  # noqa: S603 —— 参数以列表传入，不经 shell
            args,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"探测超时（超过 {timeout_seconds} 秒未返回），文件可能过大或磁盘读取异常") from exc
    except FileNotFoundError as exc:
        raise ProbeError(f"无法执行 ffprobe（{ffprobe_path}）：{exc}") from exc
    except OSError as exc:  # noqa: BLE001 —— 权限、路径过长等系统级问题
        raise ProbeError(f"执行 ffprobe 失败：{exc}") from exc

    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")

    if len(completed.stdout) > max_output_bytes:
        raise ProbeError(
            f"ffprobe 输出超过上限（{max_output_bytes} 字节），文件可能不是可识别的媒体格式"
        )

    # 退出码优先于解析：stderr 里 ffprobe 给出的原因比「JSON 不完整」更接近真正的问题
    if completed.returncode != 0:
        detail = _excerpt(stderr) or "ffprobe 未给出错误详情"
        raise ProbeError(f"ffprobe 探测失败（退出码 {completed.returncode}）：{detail}")

    try:
        metadata = parse_probe_output(stdout)
    except ProbeParseError as exc:
        # 解析失败时带上 stderr 摘要：ffprobe 有时把警告写在 stderr 而 stdout 不完整
        detail = _excerpt(stderr)
        suffix = f"（ffprobe 提示：{detail}）" if detail else ""
        raise ProbeError(f"{exc}{suffix}") from exc

    if not has_playable_streams(metadata):
        raise ProbeError("文件中没有音频或视频流，无法作为录屏素材处理")

    return metadata
