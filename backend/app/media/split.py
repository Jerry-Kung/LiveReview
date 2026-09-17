"""切分：按「时长上限 + 体积上限」双约束生成切分计划，并执行单片切分与校验。

计划是纯函数（不碰文件系统、不起子进程），可以在本地用固定数字充分测试；执行部分只负责
把计划条目交给 ffmpeg，并校验产物确实是可用的音视频片段。

顺序约束：片段顺序由计划列表的顺序定义，序号即下标，切分与上传都按该顺序推进。体积超限时
在**原位置**递归对半展开，因此「第 N 片的子片段」仍然排在相邻位置，不会打乱时间轴顺序。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.media.ffmpeg import FFmpegError, run_ffmpeg
from app.media.ffprobe import MediaMetadata
from app.media.probe import ProbeError, probe_file

logger = logging.getLogger(__name__)

# 时间点精度：ffmpeg 的 -ss/-to 支持毫秒，计划值也按毫秒取整，避免浮点尾差导致相邻片重叠
_TIME_PRECISION = 1000

# 单片产物时长容差：-c copy 的切点必须落在关键帧上，实际长度与计划值的偏差由此而来
CLIP_DURATION_SLACK_SECONDS = 5.0

# 单片切分参数：只换容器不改编码，与转封装保持同一套「不重编码、不丢音频」的约定。
#
# 输入侧与输出侧分开：`-ss` 写在 `-i` 之前是快速定位（直接跳到目标位置的关键帧附近，
# GB 级文件不必先解复用整段），`-to` 与流映射属输出侧。写错位置 ffmpeg 会直接拒绝执行。
CLIP_INPUT_ARGS_TEMPLATE: tuple[str, ...] = (
    "-v",
    "error",
    "-ss",
    "{start}",
)

CLIP_ARGS_TEMPLATE: tuple[str, ...] = (
    # -to 以输入时间轴为基准，与 -ss 组合即 [start, end) 区间
    "-to",
    "{end}",
    "-map",
    "0:v",
    "-map",
    "0:a?",
    "-c",
    "copy",
    # 起点前移产生负时间戳时统一挪到 0，避免片段开头出现不可解码的一段
    "-avoid_negative_ts",
    "make_zero",
    "-movflags",
    "+faststart",
)


class SplitError(Exception):
    """切分失败：原因可直接作为任务的失败信息展示与入库。"""


# 时间区间是片段的稳定标识：序号会因拆分而改变，起止时间不会
_TIME_MATCH_TOLERANCE_SECONDS = 0.002


@dataclass(frozen=True)
class ClipPlan:
    """一个计划中的片段：序号即其在时间轴上的顺序，不可重排。"""

    index: int
    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def to_input_args(self) -> list[str]:
        """输入侧参数（`-i` 之前）：快速定位到片段起点。"""
        return _fill(CLIP_INPUT_ARGS_TEMPLATE, self)

    def to_args(self) -> list[str]:
        """输出侧参数（`-i` 之后）：截到片段终点并按流复制封装为 MP4。"""
        return _fill(CLIP_ARGS_TEMPLATE, self)


def _format_time(seconds: float) -> str:
    """按毫秒精度格式化时间点；ffmpeg 接受十进制秒。"""
    return f"{max(0.0, seconds):.3f}"


def _fill(template: Sequence[str], clip: "ClipPlan") -> list[str]:
    """把模板里的 `{start}` / `{end}` 占位换成该片段的时间点；未出现的占位原样跳过。"""
    replacements = {
        "{start}": _format_time(clip.start_seconds),
        "{end}": _format_time(clip.end_seconds),
    }
    return [replacements.get(token, token) for token in template]


def _round_time(seconds: float) -> float:
    return round(max(0.0, seconds) * _TIME_PRECISION) / _TIME_PRECISION


def _measured_size(measured: Sequence[tuple[float, float, int]], plan: ClipPlan) -> int | None:
    """按起止时间在实测结果里找对应体积；序号不作判据（拆分后序号会变）。"""
    for start, end, size in measured:
        if (
            abs(start - plan.start_seconds) <= _TIME_MATCH_TOLERANCE_SECONDS
            and abs(end - plan.end_seconds) <= _TIME_MATCH_TOLERANCE_SECONDS
        ):
            return size
    return None


def plan_initial_clips(total_duration_seconds: float, max_duration_seconds: float) -> list[ClipPlan]:
    """按最大时长把整场均分为若干片段；余数并入最后一片。

    余数并入末尾（而不是单独切出一个极短尾巴）让片段长度更接近均匀，也避免为几秒的内容
    产生一个额外的片段对象。
    """
    if total_duration_seconds <= 0:
        raise SplitError(f"视频时长无效（{total_duration_seconds}），无法切分")
    if max_duration_seconds <= 0:
        raise SplitError(
            f"单片时长上限无效（{max_duration_seconds}），请检查 SPLIT_MAX_DURATION_SECONDS"
        )

    count = max(1, math.ceil(total_duration_seconds / max_duration_seconds))
    if count == 1:
        return [
            ClipPlan(index=0, start_seconds=0.0, end_seconds=_round_time(total_duration_seconds))
        ]

    # 按「总时长 ÷ 片段数」均分，使每片长度一致且都不超过上限
    segment = _round_time(total_duration_seconds / count)
    clips: list[ClipPlan] = []
    for position in range(count - 1):
        start = _round_time(position * segment)
        clips.append(
            ClipPlan(index=position, start_seconds=start, end_seconds=_round_time(start + segment))
        )
    last = clips[-1].end_seconds
    clips.append(
        ClipPlan(
            index=count - 1, start_seconds=last, end_seconds=_round_time(total_duration_seconds)
        )
    )
    return clips


def refine_by_size(
    clips: Sequence[ClipPlan],
    *,
    max_clip_bytes: int,
    measured: Sequence[tuple[float, float, int]],
    min_clip_seconds: float = 1.0,
) -> list[ClipPlan]:
    """把实测体积超限的片段在原位置递归对半，直到全部满足体积上限。

    `measured` 是上一轮切分得到的 `(起, 止, 字节数)` 列表。未实测到的片段按「无法判断」
    处理并原样保留：不臆造体积，避免把未知当成已知。

    体积超限的片段在原位置展开为两半，因此**顺序不变**；对半到 `min_clip_seconds` 仍超限
    说明码率高到无法同时满足两个约束，直接失败而不是继续细分。
    """
    if max_clip_bytes <= 0:
        raise SplitError(
            f"单片体积上限无效（{max_clip_bytes}），请检查 SPLIT_MAX_CLIP_BYTES"
        )

    resolved: list[ClipPlan] = []
    for clip in clips:
        resolved.extend(
            _shrink_to_fit(
                clip,
                sizes=measured,
                max_clip_bytes=max_clip_bytes,
                min_clip_seconds=min_clip_seconds,
            )
        )
    return [
        ClipPlan(index=index, start_seconds=clip.start_seconds, end_seconds=clip.end_seconds)
        for index, clip in enumerate(resolved)
    ]


def _shrink_to_fit(
    clip: ClipPlan,
    *,
    sizes: Sequence[tuple[float, float, int]],
    max_clip_bytes: int,
    min_clip_seconds: float,
) -> list[ClipPlan]:
    size = _measured_size(sizes, clip)
    if size is None or size <= max_clip_bytes:
        return [clip]

    if clip.duration_seconds < 2 * min_clip_seconds:
        raise SplitError(
            f"片段（{clip.start_seconds:.1f}s ~ {clip.end_seconds:.1f}s）体积 {size} 字节超过上限 "
            f"{max_clip_bytes} 字节，且已细分到 {min_clip_seconds} 秒下限，无法在满足体积约束的"
            "同时保留可用片段"
        )

    midpoint = _round_time(clip.start_seconds + clip.duration_seconds / 2)
    if midpoint <= clip.start_seconds or midpoint >= clip.end_seconds:
        raise SplitError(
            f"片段无法继续细分（{clip.start_seconds:.3f}s ~ {clip.end_seconds:.3f}s）"
        )

    halves = [
        ClipPlan(index=clip.index, start_seconds=clip.start_seconds, end_seconds=midpoint),
        ClipPlan(index=clip.index, start_seconds=midpoint, end_seconds=clip.end_seconds),
    ]
    # 对半后的体积按实测比例估算：同码率下体积与时长近似成正比，据此判断哪一半仍需细分。
    # 估算值只用于「要不要继续细分」的决策，片段的实际体积始终由切分后实测得出。
    estimated = [
        (
            half.start_seconds,
            half.end_seconds,
            int(size * half.duration_seconds / clip.duration_seconds),
        )
        for half in halves
    ]
    resolved: list[ClipPlan] = []
    for half in halves:
        resolved.extend(
            _shrink_to_fit(
                half,
                sizes=estimated,
                max_clip_bytes=max_clip_bytes,
                min_clip_seconds=min_clip_seconds,
            )
        )
    return resolved


def plan_clips(
    total_duration_seconds: float,
    *,
    max_duration_seconds: float,
    max_clip_bytes: int,
    min_clip_seconds: float = 1.0,
    measured: Sequence[tuple[float, float, int]] = (),
) -> list[ClipPlan]:
    """完整切分计划：先按时长均分，再按实测体积递归对半（若已有实测数据）。"""
    clips = plan_initial_clips(total_duration_seconds, max_duration_seconds)
    if not measured:
        return clips
    return refine_by_size(
        clips,
        max_clip_bytes=max_clip_bytes,
        measured=measured,
        min_clip_seconds=min_clip_seconds,
    )


def validate_clip(
    metadata: MediaMetadata,
    *,
    planned: ClipPlan,
    size_bytes: int,
) -> None:
    """校验单片产物：必须有画面、必须有声音、时长与计划基本吻合。

    音频缺失一律判失败：切分出的无声片段对「录屏复盘」没有价值，宁可让任务失败暴露问题，
    也不静默产出一批没有声音的片段。

    **不做体积判定**：单片体积要靠实测才知道，超出上限的片段需要「二分重切」而不是直接
    失败，这个收敛过程由任务体负责（见 `tasks/runner._converge_on_size`）。
    """
    if metadata.video is None:
        raise SplitError(
            f"片段 {planned.index}（{planned.start_seconds:.1f}s ~ {planned.end_seconds:.1f}s）"
            "没有视频流，切分未产出有效画面"
        )
    if metadata.audio is None:
        raise SplitError(
            f"片段 {planned.index}（{planned.start_seconds:.1f}s ~ {planned.end_seconds:.1f}s）"
            "没有音频流，切分过程中丢失了声音"
        )
    if size_bytes <= 0:
        raise SplitError(f"片段 {planned.index} 的体积为 0，切分未产出有效内容")

    duration = metadata.duration_seconds
    if duration is None:
        return
    lower = planned.duration_seconds - CLIP_DURATION_SLACK_SECONDS
    upper = planned.duration_seconds + CLIP_DURATION_SLACK_SECONDS
    if not lower <= duration <= upper:
        raise SplitError(
            f"片段 {planned.index} 实际时长 {duration:.1f}s 与计划 "
            f"{planned.duration_seconds:.1f}s 偏差过大（容差 {CLIP_DURATION_SLACK_SECONDS:.0f}s）"
        )


def cut_clip(
    source: str | Path,
    output: str | Path,
    *,
    planned: ClipPlan,
    ffmpeg_command: str | Sequence[str],
    ffprobe_command: str | Sequence[str],
    timeout_seconds: int = 600,
    probe_timeout_seconds: int = 300,
) -> MediaMetadata:
    """切出一个片段并做内容校验（画面、声音、时长），返回产物的元数据。"""
    logger.info(
        "切分第 %d 片：%.3fs ~ %.3fs", planned.index, planned.start_seconds, planned.end_seconds
    )
    run_ffmpeg(
        ffmpeg_command,
        source=source,
        output=output,
        input_args=planned.to_input_args(),
        extra_args=planned.to_args(),
        timeout_seconds=timeout_seconds,
    )

    output_path = Path(output)
    try:
        metadata = probe_file(
            output_path,
            ffprobe_path=ffprobe_command,
            timeout_seconds=probe_timeout_seconds,
        )
    except ProbeError as exc:
        raise SplitError(f"片段 {planned.index} 无法探测：{exc}") from exc

    validate_clip(metadata, planned=planned, size_bytes=output_path.stat().st_size)
    return metadata
