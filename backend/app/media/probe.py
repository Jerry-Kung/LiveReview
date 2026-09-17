"""ffprobe 子进程调用：探测一个本地媒体文件。

调用约定：以参数列表传参（不经 shell），带超时与输出长度上限；非零退出码、超时、
可执行文件缺失、输出无法解析都映射为 `ProbeError`，由任务体写入任务的失败原因。
本模块不落库、不感知对象存储，只回答「这个文件是什么媒体」。

除整份元数据外，这里还提供 `measure_discontinuities()`：逐包读时间戳，量出源素材里
源于直播推流的固有时间戳断层。转封装会把断层抹平（产物时长因此合法地变短），这个量
是判断「产物变短」到底是截断还是抹平的唯一依据。
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from app.media.ffprobe import MediaMetadata, ProbeParseError, has_playable_streams, parse_probe_output

logger = logging.getLogger(__name__)

# stderr 摘要长度：失败原因要能入库并展示给用户，但不该把整段 stderr 塞进数据库
STDERR_EXCERPT_LENGTH = 500

# 逐包探测（时间戳断层）的输出形态：`流下标:时间戳`，一行一个包
PACKET_FIELD_SEPARATOR = ":"

# 时间戳断层阈值：正常包间隔在数十毫秒量级，直播推流抖动留下的断层是秒级
DISCONTINUITY_THRESHOLD_SECONDS = 0.5

# 逐包输出按字节截断的上限：GB 级录屏的逐包清单可达数十 MB，超限即停止统计
PACKET_SCAN_MAX_BYTES = 64 * 1024 * 1024


class ProbeError(Exception):
    """探测失败：原因可直接作为任务的失败信息展示与入库。"""


@dataclasses.dataclass(frozen=True)
class TimelineReport:
    """一次逐包扫描的结论：可播放时长与其中的空洞。

    `media_seconds` 是这份文件**真正有多少内容**：把逐包时间戳排序后累加相邻间隔得到，
    时间戳断层（直播断流留下的空洞）天然不计入，因此它是源与产物之间唯一可比的量——
    容器标称时长会把空洞算进去，拿它比较必然得出「产物被截断」的误判。
    """

    media_seconds: float
    gap_seconds: float
    gap_count: int
    measured: bool

    def summary(self) -> str:
        if not self.measured:
            return "未量得时间轴"
        if self.gap_count == 0:
            return f"可播放 {self.media_seconds:.1f}s，时间戳连续"
        return (
            f"可播放 {self.media_seconds:.1f}s，"
            f"其中 {self.gap_count} 处断层共 {self.gap_seconds:.1f}s"
        )


UNMEASURED_TIMELINE = TimelineReport(
    media_seconds=0.0, gap_seconds=0.0, gap_count=0, measured=False
)


def build_ffprobe_args(
    ffprobe_command: str | Sequence[str],
    source: str | Path,
    *,
    select_streams: str | None = None,
) -> list[str]:
    """构造 ffprobe 参数。

    命令可以是单个可执行文件名，也可以是「解释器 + 脚本」形式的序列（测试环境用假
    ffprobe 脚本时采用后者）。`-count_packets` 让 TS 等容器头无 duration 的录屏也能算出
    时长（V0.1.5 的时间基准依赖该值）；`-v error` 只让真正的错误进入 stderr。

    `select_streams` 用于只统计某一条流（如 `v:0`）：逐包探测要按流分别量断层，混在一起
    会把同一处断层在音视频两路上各算一遍。
    """
    prefix = [ffprobe_command] if isinstance(ffprobe_command, str) else list(ffprobe_command)
    if not prefix:
        raise ProbeError("ffprobe 命令为空，无法探测")

    args = [
        *prefix,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-count_packets",
    ]
    if select_streams:
        args += ["-select_streams", select_streams]
    return [*args, str(source)]


def _packet_scan_args(
    ffprobe_command: str | Sequence[str],
    source: str | Path,
    *,
    select_streams: str | None = None,
) -> list[str]:
    """逐包时间戳清单的参数：只要时间戳，不要整份 JSON。"""
    prefix = [ffprobe_command] if isinstance(ffprobe_command, str) else list(ffprobe_command)
    if not prefix:
        raise ProbeError("ffprobe 命令为空，无法探测")

    args = [
        *prefix,
        "-v",
        "error",
        "-print_format",
        "csv=p=0",
        "-show_entries",
        "packet=pts_time",
    ]
    if select_streams:
        args += ["-select_streams", select_streams]
    return [*args, str(source)]


def _precheck_ffprobe(ffprobe_path: str | Sequence[str]) -> None:
    """只对「裸可执行文件名」做存在性预检：绝对路径与「解释器 + 脚本」形式由调用方负责。"""
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


def measure_timeline(
    source: str | Path,
    *,
    ffprobe_path: str | Sequence[str] = "ffprobe",
    select_streams: str | None = None,
    timeout_seconds: int = 300,
    threshold_seconds: float = DISCONTINUITY_THRESHOLD_SECONDS,
    max_scan_bytes: int = PACKET_SCAN_MAX_BYTES,
) -> TimelineReport:
    """量出一份文件的可播放时长与其中的时间戳断层。

    逐包读 PTS 并流式累加，不把整份清单读进内存：GB 级录屏的包数以万计。累加用的是
    「到目前为止的最大 PTS」而不是相邻两包——B 帧会让 PTS 乱序（本项目那份 765MB 素材
    里有近半数包是回退的），拿相邻包相减会得出大量仅 0.04s 的伪影。只有明显超过正常包
    间隔的向前跳变才算断层，并单独记入 `gap_seconds` 而不计入 `media_seconds`。

    返回的 `measured` 为 False 表示这次没量成（路径不存在、ffprobe 失败、超时、清单被
    截断）：调用方必须把它当成「不知道」，而不是「没有断层」。
    """
    path = Path(source)
    if not path.is_file():
        raise ProbeError(f"待探测的文件不存在或不是普通文件：{path.name}")

    _precheck_ffprobe(ffprobe_path)

    args = _packet_scan_args(ffprobe_path, path, select_streams=select_streams)
    logger.info("统计时间轴：%s", " ".join(args))

    media_seconds = 0.0
    gap_seconds = 0.0
    gap_count = 0
    first: float | None = None
    last: float | None = None
    previous_max: float | None = None
    scanned = 0
    truncated = False

    try:
        process = subprocess.Popen(  # noqa: S603 —— 参数以列表传入，不经 shell
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ProbeError(f"无法执行 ffprobe（{ffprobe_path}）：{exc}") from exc
    except OSError as exc:  # noqa: BLE001 —— 权限、路径过长等系统级问题
        raise ProbeError(f"执行 ffprobe 失败：{exc}") from exc

    returncode: int | None = None
    timed_out = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            scanned += len(raw_line)
            if scanned > max_scan_bytes:
                truncated = True
                break
            timestamp = _parse_packet_timestamp(raw_line)
            if timestamp is None:
                continue
            if first is None:
                first = timestamp
            if previous_max is not None and timestamp - previous_max > threshold_seconds:
                # 超过阈值的一段是空洞：只记断层，不计入可播放时长
                gap_seconds += timestamp - previous_max
                gap_count += 1
            if previous_max is None or timestamp > previous_max:
                previous_max = timestamp
                last = timestamp

        if truncated:
            # 提前收工：先终止子进程再读回退出码，否则等的是一份已经不需要的完整清单
            process.kill()
            process.wait()
            returncode = 0
        else:
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    if timed_out:
        raise ProbeError(
            f"统计时间轴超时（超过 {timeout_seconds} 秒未返回），文件可能过大或磁盘读取异常"
        )
    if returncode != 0:
        raise ProbeError(f"统计时间轴失败（退出码 {returncode}）")
    if truncated:
        # 截断的统计会漏掉后半段的内容，宁可当成「没量到」，也不能给出偏短的结论
        logger.warning("逐包清单超过 %d 字节，已截断，时间轴统计不可用", max_scan_bytes)
        return UNMEASURED_TIMELINE

    if first is None or last is None:
        # 一个带时间戳的包都没有：这份清单说明不了任何事
        return UNMEASURED_TIMELINE

    media_seconds = (last - first) - gap_seconds
    return TimelineReport(
        media_seconds=media_seconds,
        gap_seconds=gap_seconds,
        gap_count=gap_count,
        measured=True,
    )


def _parse_packet_timestamp(raw_line: bytes) -> float | None:
    """解析逐包清单的一行：`流下标:时间戳` 或只有时间戳。

    ffprobe 的 csv 输出字段带引号且以逗号分隔（`"0:58.206000",`），先统一剥掉；时间戳
    也可能缺值（ffprobe 对无 PTS 的包输出空字段），解析不出来就跳过该包。
    """
    text = raw_line.decode("utf-8", errors="replace").strip().strip(",").strip('"').strip()
    if not text:
        return None
    _, _, value = text.rpartition(PACKET_FIELD_SEPARATOR)
    value = value.strip().strip('"').strip()
    if not value:
        return None
    try:
        timestamp = float(value)
    except ValueError:
        return None
    if not math.isfinite(timestamp) or timestamp < 0:
        return None
    return timestamp


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

    _precheck_ffprobe(ffprobe_path)

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
