"""预处理：把原始录屏统一转封装为 MP4，作为切分的输入。

为什么不重编码：录屏码率本身已满足需求，转封装只换容器、不改动音视频数据，
GB 级文件几分钟即可完成；重编码会二次劣化画面并显著拉长耗时。代价是时间戳异常的源
转封装后仍可能错位，因此转换后必须校验产物（时长、流数、音频流），不合格即判失败。

时长校验为什么要改为比「可播放时长」：直播录屏中途断流会在源 TS 里留下数秒到数十秒的
PTS 空洞，转封装会把这些空洞**正确地**抹平，产物因此合法地比容器的标称时长短。用容器
时长做基准就必然把这种情况误判成截断（V0.2 那份 1683.2s 素材正是如此：源里 5 处断层共
45.2s，标称时长把空洞算了进去，实测可播放时长 1638.1s，与产物的 1638.2s 几乎相同）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from app.media.ffmpeg import FFmpegError, run_ffmpeg
from app.media.ffprobe import MediaMetadata
from app.media.probe import ProbeError, measure_timeline, probe_file

logger = logging.getLogger(__name__)

# 视为 MP4 的容器名：ffprobe 对 mp4/mov 系列返回 "mov,mp4,m4a,3gp,3g2,mj2"
_MP4_FORMAT_TOKENS = ("mp4", "mov", "m4a", "3gp", "3g2", "mj2")

# 转封装产物校验容差：流复制不重新编码，时长偏差只应来自时间基换算与首尾裁剪
DURATION_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_RATIO = 0.005

# 参数：只换容器，不改编码，丢弃数据流之外的附加轨道（如 TS 里的数据/字幕流）
#
# 输入侧与输出侧必须分开：ffmpeg 的选项只作用于紧随其后的那个文件，写错位置会直接报
# 「Option map cannot be applied to input url」——V0.1.5 首次在测试环境跑真实 ffmpeg
# 时正是这样失败的。输入侧放解复用选项，输出侧放流映射与封装选项。
REMUX_INPUT_ARGS: tuple[str, ...] = (
    "-v",
    "error",
    # TS 常带时间戳断层，在解复用阶段补生成 PTS，避免产物时间轴不连续导致音画错位
    "-fflags",
    "+genpts",
)

REMUX_ARGS: tuple[str, ...] = (
    # 保留全部音视频流：音频是复盘的核心信息，转封装绝不主动丢流
    "-map",
    "0:v",
    "-map",
    "0:a?",
    "-c",
    "copy",
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


def duration_tolerance(original_duration: float) -> float:
    """时长容差：按源时长比例放宽，但不低于绝对下限。"""
    return max(DURATION_TOLERANCE_SECONDS, original_duration * DURATION_TOLERANCE_RATIO)


def validate_converted(
    original: MediaMetadata,
    converted: MediaMetadata,
    *,
    original_media_seconds: float | None = None,
    converted_media_seconds: float | None = None,
) -> None:
    """校验转封装产物是否仍然是一份可用的完整媒体。

    三类问题直接判失败，不做静默降级：时长明显偏离（截断或时间轴异常）、丢流、
    丢音频。音频尤其重要——直播复盘依赖语音内容，静默产出无声片段比失败更糟。

    时长比对的基准是**可播放时长**（`*_media_seconds`，不含时间戳空洞）而不是容器标称
    时长：直播断流会在源里留下数十秒的空洞，转封装把它们抹平属于正常，用标称时长比必然
    误判成截断。调用方量不出可播放时长时传 None，退回标称时长的比对。
    """
    if converted.video is None:
        raise FFmpegError("转封装产物没有视频流，转换过程中丢失了画面")

    if original.audio is not None and converted.audio is None:
        raise FFmpegError("转封装产物没有音频流，转换过程中丢失了声音")

    if converted.format_name and not is_target_container(converted.format_name):
        raise FFmpegError(
            f"转封装产物容器仍不是 MP4（实际 {converted.format_name}），转换未生效"
        )

    if original_media_seconds is not None and converted_media_seconds is not None:
        baseline, actual, basis = original_media_seconds, converted_media_seconds, "可播放时长"
    else:
        baseline, actual, basis = original.duration_seconds, converted.duration_seconds, "容器标称时长"
        logger.warning("未能量出可播放时长，本次按容器标称时长比对，容差取最严")

    if baseline is None:
        logger.warning("原始视频时长未知，跳过转封装产物的时长比对")
        return
    if actual is None:
        raise FFmpegError("转封装产物无法读出时长，产物可能已损坏")

    # 短于源才是问题（截断）；略长只可能来自首尾对齐的取整，放宽到容差之外才判失败
    budget = duration_tolerance(baseline)
    if baseline - actual > budget:
        raise FFmpegError(
            f"转封装产物时长与原始视频偏差过大（源的{basis} {baseline:.1f}s，"
            f"产物 {actual:.1f}s，容差 {budget:.1f}s），产物可能被截断"
        )


def _timeline_seconds(
    path: str | Path,
    *,
    ffprobe_command: str | Sequence[str],
    timeout_seconds: int,
    original: MediaMetadata,
) -> tuple[float | None, str]:
    """量一份文件的**可播放时长**（不含时间戳空洞）。

    这是源与产物之间唯一可比的量。容器标称时长会把直播断流留下的空洞算进去，拿它比较必然
    把「断层被抹平」误判成「产物被截断」——线上那份 1683.2s 素材就是这样失败的。

    逐条流各量一遍取较大值。流的写法是 ffprobe 的 `-select_streams` 语法：`v:0` / `a:0`
    指「第 0 条视频/音频流」，写成 `0:v` 会被 ffprobe 判为非法流选择符而整体失败。
    量不出来时返回 None，由调用方退回标称时长的比对，而不是当作 0。
    """
    selectors: list[str] = []
    if original.video is not None:
        selectors.append("v:0")
    if original.audio is not None:
        selectors.append("a:0")
    if not selectors:
        return None, "源素材没有可统计的流"

    measured: list[float] = []
    details: list[str] = []
    for selector in selectors:
        try:
            report = measure_timeline(
                path,
                ffprobe_path=ffprobe_command,
                select_streams=selector,
                timeout_seconds=timeout_seconds,
            )
        except ProbeError as exc:
            logger.warning("统计 %s 的时间轴失败：%s", selector, exc)
            details.append(f"{selector} 统计失败（{exc}）")
            continue
        if not report.measured:
            details.append(f"{selector} 未量得时间轴")
            continue
        measured.append(report.media_seconds)
        details.append(f"{selector} {report.summary()}")

    if not measured:
        return None, "；".join(details) or "无法统计时间轴"
    return max(measured), "；".join(details)


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
        input_args=REMUX_INPUT_ARGS,
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

    original_media, original_detail = _timeline_seconds(
        source,
        ffprobe_command=ffprobe_command,
        timeout_seconds=probe_timeout_seconds,
        original=original_metadata,
    )
    converted_media, converted_detail = _timeline_seconds(
        output,
        ffprobe_command=ffprobe_command,
        timeout_seconds=probe_timeout_seconds,
        original=converted,
    )
    validate_converted(
        original_metadata,
        converted,
        original_media_seconds=original_media,
        converted_media_seconds=converted_media,
    )
    logger.info(
        "时间轴统计：源 %s（%s）；产物 %s（%s）",
        "未量得" if original_media is None else f"{original_media:.1f}s",
        original_detail,
        "未量得" if converted_media is None else f"{converted_media:.1f}s",
        converted_detail,
    )
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
