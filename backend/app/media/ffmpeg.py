"""ffmpeg 子进程调用：转封装与切分共用的执行层。

调用约定与 `probe.py` 一致：以参数列表传参（不经 shell），带超时与输出长度上限；非零
退出码、超时、可执行文件缺失都映射为 `FFmpegError`，由任务体写入任务的失败原因。
本模块不落库、不感知对象存储，只回答「这条 ffmpeg 命令有没有跑成、产物有没有落地」。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# 失败原因要能入库并展示给用户，但不该把整段 stderr 塞进数据库
STDERR_EXCERPT_LENGTH = 500


class FFmpegError(Exception):
    """ffmpeg 执行失败：原因可直接作为任务的失败信息展示与入库。"""


def command_prefix(command: str | Sequence[str]) -> list[str]:
    """把配置里的工具路径规范成参数前缀。

    命令可以是单个可执行文件名，也可以是「解释器 + 脚本」形式的序列（测试环境用假
    ffmpeg 脚本时采用后者）。
    """
    prefix = [command] if isinstance(command, str) else list(command)
    if not prefix or not str(prefix[0]).strip():
        raise FFmpegError("ffmpeg 命令为空，无法执行")
    return [str(part) for part in prefix]


def build_ffmpeg_args(
    ffmpeg_command: str | Sequence[str],
    *,
    source: str | Path,
    output: str | Path,
    input_args: Sequence[str] = (),
    extra_args: Sequence[str] = (),
    overwrite: bool = True,
) -> list[str]:
    """构造 ffmpeg 参数：全局开关 → 输入选项 → 输入 → 输出选项 → 输出。

    ffmpeg 的位置敏感是这条命令最容易踩的坑：选项只作用于「紧随其后的那个文件」，
    输入选项写到 `-i` 之后会被当成输出选项，反之亦然。混放会直接以
    `Option map ... cannot be applied to input url` 之类的原因失败——因此这里把
    输入侧与输出侧显式分开，调用方按 ffmpeg 的语义各归其位：

    - `input_args`：作用于输入的解复用选项（如 `-ss` 快速定位、`-fflags +genpts`）；
    - `extra_args`：作用于输出的选项（如 `-map`、`-c copy`、`-movflags`）。

    `-nostdin` 防止 ffmpeg 从父进程的标准输入读取交互命令。
    """
    args = [*command_prefix(ffmpeg_command), "-hide_banner", "-nostdin"]
    if overwrite:
        args.append("-y")
    args += [*input_args, "-i", str(source), *extra_args, str(output)]
    return args


def _excerpt(text: str) -> str:
    excerpt = (text or "").strip().replace("\n", " ")
    if len(excerpt) <= STDERR_EXCERPT_LENGTH:
        return excerpt
    return excerpt[:STDERR_EXCERPT_LENGTH] + "…"


def _precheck_command(ffmpeg_command: str | Sequence[str]) -> None:
    """只对「裸可执行文件名」做存在性预检：绝对路径与「解释器 + 脚本」形式由调用方负责。"""
    if not isinstance(ffmpeg_command, str):
        return
    name = ffmpeg_command
    if not name or os.sep in name or "/" in name or (os.altsep and os.altsep in name):
        return
    if shutil.which(name) is None:
        raise FFmpegError(
            f"未找到 ffmpeg（{name}）：本地不部署 FFmpeg，转封装与切分需在测试环境执行，"
            "或通过 FFMPEG_PATH 指定可执行文件路径"
        )


def run_ffmpeg(
    ffmpeg_command: str | Sequence[str],
    *,
    source: str | Path,
    output: str | Path,
    input_args: Sequence[str] = (),
    extra_args: Sequence[str] = (),
    timeout_seconds: int = 600,
    max_output_bytes: int = 8 * 1024 * 1024,
    cleanup_on_failure: bool = True,
) -> Path:
    """执行一次 ffmpeg 调用并返回产物路径。

    成功时校验产物确实存在且非空——ffmpeg 退出码为 0 却写出空文件的情况（磁盘满、编码器
    异常）必须当失败处理，否则下游会拿着空片段继续上传。

    产物体积不由本函数判断：「多大算正常」取决于源文件与切片参数，与 ffmpeg 的日志长度
    无关；体积约束由切分层用配置的上限校验（见 `split.validate_clip`）。
    `max_output_bytes` 限制的是收集回来的 stderr 长度，避免异常输入把日志撑爆内存。

    `input_args` 与 `extra_args` 的区别见 `build_ffmpeg_args`：前者是输入侧选项，
    后者是输出侧选项，写错位置 ffmpeg 会直接拒绝执行。
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise FFmpegError(f"待处理的文件不存在或不是普通文件：{source_path.name}")

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    _precheck_command(ffmpeg_command)

    args = build_ffmpeg_args(
        ffmpeg_command,
        source=source_path,
        output=output_path,
        input_args=input_args,
        extra_args=extra_args,
    )
    logger.info("ffmpeg 调用：%s", " ".join(args))
    try:
        completed = subprocess.run(  # noqa: S603 —— 参数以列表传入，不经 shell
            args,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _discard(output_path, cleanup_on_failure)
        raise FFmpegError(
            f"ffmpeg 执行超时（超过 {timeout_seconds} 秒未返回），文件可能过大或磁盘读取异常"
        ) from exc
    except FileNotFoundError as exc:
        raise FFmpegError(f"无法执行 ffmpeg（{ffmpeg_command}）：{exc}") from exc
    except OSError as exc:  # noqa: BLE001 —— 权限、路径过长等系统级问题
        raise FFmpegError(f"执行 ffmpeg 失败：{exc}") from exc

    if len(completed.stderr) > max_output_bytes:
        logger.warning(
            "ffmpeg stderr 输出超过 %d 字节，失败原因只截取前 %d 字节",
            max_output_bytes,
            STDERR_EXCERPT_LENGTH,
        )
    stderr = completed.stderr[:max_output_bytes].decode("utf-8", errors="replace")

    if completed.returncode != 0:
        _discard(output_path, cleanup_on_failure)
        detail = _excerpt(stderr) or "ffmpeg 未给出错误详情"
        raise FFmpegError(f"ffmpeg 执行失败（退出码 {completed.returncode}）：{detail}")

    if not output_path.is_file() or output_path.stat().st_size == 0:
        _discard(output_path, cleanup_on_failure)
        detail = _excerpt(stderr)
        suffix = f"（ffmpeg 提示：{detail}）" if detail else ""
        raise FFmpegError(f"ffmpeg 未产出可用的输出文件：{output_path.name}{suffix}")

    return output_path


def _discard(path: Path, enabled: bool) -> None:
    """失败时删除半成品：留下残片会让下一次执行或覆盖校验把残片当成有效产物。"""
    if not enabled:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("失败产物清理失败：%s（%s）", path, exc)
