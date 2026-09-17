"""预处理：把原始录屏统一转封装为 MP4，作为切分的输入。

为什么不重编码：录屏码率本身已满足需求，转封装只换容器、不改动音视频数据，
GB 级文件几分钟即可完成；重编码会二次劣化画面并显著拉长耗时。代价是时间戳异常的源
转封装后仍可能错位，因此转换后必须校验产物（时长、流数、音频流），不合格即判失败。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from app.media.ffmpeg import FFmpegError, run_ffmpeg
from app.media.ffprobe import MediaMetadata
from app.media.probe import ProbeError, probe_file

logger = logging.getLogger(__name__)

# 视为 MP4 的容器名：ffprobe 对 mp4/mov 系列返回 "mov,mp4,m4a,3gp,3g2,mj2"
_MP4_FORMAT_TOKENS = ("mp4", "mov", "m4a", "3gp", "3g2", "mj2")

# 转封装产物校验容差：流复制不重新编码，时长偏差只应来自时间基换算与首尾裁剪
DURATION_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_RATIO = 0.005

# 参数：只换容器，不改编码，丢弃数据流之外的附加轨道（如 TS 里的数据/字幕流）
REMUX_ARGS: tuple[str, ...] = (
    "-v",
    "error",
    # 保留全部音视频流：音频是复盘的核心信息，转封装绝不主动丢流
    "-map",
    "0:v",
    "-map",
    "0:a?",
    "-c",
    "copy",
    # TS 常带时间戳断层，补生成 PTS 让 MP4 的时间轴连续，避免音画错位
    "-fflags",
    "+genpts",
    "-avoid_negative_ts",
    "make_zero",
    # faststart 把索引前置，前端 <video> 不必下载整个文件就能起播
    "-movflags",
    "+faststart",
)


def is_target_container(format_name: str | None) -> bool:
    """探测得到的容器名是否已是 MP4 系列（无需转封装）。"""
    text = (format_name or "").lower()
    return any(token in text for token in _MP4_FORMAT_TOKENS)


def validate_converted(original: MediaMetadata, converted: MediaMetadata) -> None:
    """校验转封装产物是否仍然是一份可用的完整媒体。

    三类问题直接判失败，不做静默降级：时长明显偏离（截断或时间轴异常）、丢流、
    丢音频。音频尤其重要——直播复盘依赖语音内容，静默产出无声片段比失败更糟。
    """
    if converted.video is None:
        raise FFmpegError("转封装产物没有视频流，转换过程中丢失了画面")

    if original.audio is not None and converted.audio is None:
        raise FFmpegError("转封装产物没有音频流，转换过程中丢失了声音")

    if converted.format_name and not is_target_container(converted.format_name):
        raise FFmpegError(
            f"转封装产物容器仍不是 MP4（实际 {converted.format_name}），转换未生效"
        )

    original_duration = original.duration_seconds
    converted_duration = converted.duration_seconds
    if original_duration is None:
        logger.warning("原始视频时长未知，跳过转封装产物的时长比对")
        return
    if converted_duration is None:
        raise FFmpegError("转封装产物无法读出时长，产物可能已损坏")

    tolerance = max(DURATION_TOLERANCE_SECONDS, original_duration * DURATION_TOLERANCE_RATIO)
    if abs(converted_duration - original_duration) > tolerance:
        raise FFmpegError(
            f"转封装产物时长与原始视频偏差过大（原始 {original_duration:.1f}s，"
            f"产物 {converted_duration:.1f}s，容差 {tolerance:.1f}s），产物可能被截断"
        )


def convert_to_mp4(
    source: str | Path,
    output: str | Path,
    *,
    original_metadata: MediaMetadata,
    ffmpeg_command: str | Sequence[str],
    ffprobe_command: str | Sequence[str],
    timeout_seconds: int = 600,
    probe_timeout_seconds: int = 300,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> MediaMetadata:
    """把 `source` 转封装为 MP4 并返回**产物的**元数据。

    返回产物元数据而不是原始元数据：切分的时间基准必须来自真正拿去切分的那个文件，
    否则容器的时长差异会让最后一片的覆盖校验出现假缺口。
    """
    logger.info("转封装为 MP4：%s → %s", Path(source).name, Path(output).name)
    run_ffmpeg(
        ffmpeg_command,
        source=source,
        output=output,
        extra_args=REMUX_ARGS,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )

    try:
        converted = probe_file(
            output,
            ffprobe_path=ffprobe_command,
            timeout_seconds=probe_timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
    except ProbeError as exc:
        raise FFmpegError(f"转封装产物无法探测：{exc}") from exc

    validate_converted(original_metadata, converted)
    logger.info("转封装完成：%s", converted.summary())
    return converted


def prepare_split_source(
    source: str | Path,
    output: str | Path,
    *,
    original_metadata: MediaMetadata,
    ffmpeg_command: str | Sequence[str],
    ffprobe_command: str | Sequence[str],
    timeout_seconds: int = 600,
    probe_timeout_seconds: int = 300,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> tuple[Path, MediaMetadata, bool]:
    """准备切分输入：已是 MP4 则原样使用，否则转封装后使用。

    返回 `(切分输入路径, 该文件的元数据, 是否发生了转换)`；调用方据第三项决定任务结束时
    是否需要清理转换产物。
    """
    source_path = Path(source)
    if is_target_container(original_metadata.format_name):
        logger.info("原始视频已是 MP4 容器（%s），跳过转封装", original_metadata.format_name)
        return source_path, original_metadata, False

    converted = convert_to_mp4(
        source_path,
        output,
        original_metadata=original_metadata,
        ffmpeg_command=ffmpeg_command,
        ffprobe_command=ffprobe_command,
        timeout_seconds=timeout_seconds,
        probe_timeout_seconds=probe_timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    return Path(output), converted, True
