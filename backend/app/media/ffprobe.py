"""ffprobe 调用的输出解析：把 ffprobe 的 JSON 转为结构化元数据。

本模块只做纯解析，不启动子进程、不落库，因此可以在本地用固定 JSON 充分测试。
字段取值遵循「拿不到就是 None」：探测结果宁可缺字段，也不臆造 0 或空串，避免下游把
未知当成已知。唯一例外是 `packet_count`/`packet_duration_seconds` 的回退逻辑，见
`_fallback_duration_seconds`。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

# ffprobe 对未知数值统一输出字符串 "N/A"
_NOT_AVAILABLE = "N/A"


class ProbeParseError(Exception):
    """ffprobe 输出无法解析为元数据。"""


@dataclass(frozen=True)
class MediaStream:
    """单条流的关键属性；本版只保留切分与展示会用到的字段。"""

    index: int
    codec_type: str
    codec_name: str | None = None
    codec_long_name: str | None = None
    profile: str | None = None

    width: int | None = None
    height: int | None = None
    frame_rate: str | None = None
    pixel_format: str | None = None
    sample_aspect_ratio: str | None = None
    display_aspect_ratio: str | None = None

    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    bit_rate: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaMetadata:
    """一次探测的结果：容器层信息 + 全部流 + 原始 JSON。"""

    format_name: str | None = None
    format_long_name: str | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    bit_rate: int | None = None
    probe_score: int | None = None
    stream_count: int | None = None
    streams: tuple[MediaStream, ...] = ()
    raw_json: str = ""

    @property
    def video(self) -> MediaStream | None:
        return self._first("video")

    @property
    def audio(self) -> MediaStream | None:
        return self._first("audio")

    def _first(self, codec_type: str) -> MediaStream | None:
        for stream in self.streams:
            if stream.codec_type == codec_type:
                return stream
        return None

    def summary(self) -> str:
        """一行摘要，用于日志与失败排查时确认探测到的是什么。"""
        parts: list[str] = []
        if self.duration_seconds is not None:
            parts.append(f"{self.duration_seconds:.1f}s")
        video = self.video
        if video is not None:
            resolution = (
                f"{video.width}x{video.height}"
                if video.width is not None and video.height is not None
                else "分辨率未知"
            )
            parts.append(f"视频 {video.codec_name or '未知编码'} {resolution}")
        audio = self.audio
        if audio is not None:
            parts.append(f"音频 {audio.codec_name or '未知编码'}")
        if self.format_name:
            parts.append(f"容器 {self.format_name}")
        return "，".join(parts) if parts else "未识别出可用的媒体信息"


def _as_int(value: Any) -> int | None:
    if value is None or value == _NOT_AVAILABLE:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == _NOT_AVAILABLE:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # 负值与 NaN 不是有效时长
    return number if number >= 0 else None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text != _NOT_AVAILABLE else None


def _fallback_duration_seconds(stream: dict[str, Any], frame_rate: str | None) -> float | None:
    """TS 等容器头无 duration 时，用帧数与帧率换算出时长。

    `-count_packets` 会给出 `nb_read_packets`；配合帧率即可得到时间基准。两者缺一时
    返回 None，由调用方决定是否判定为「时长不可用」。
    """
    packets = _as_int(stream.get("nb_read_packets"))
    if packets is None or not frame_rate:
        return None
    try:
        numerator, _, denominator = frame_rate.partition("/")
        fps = float(numerator) / float(denominator or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if fps <= 0:
        return None
    return packets / fps


def _parse_stream(raw: dict[str, Any], index: int) -> MediaStream:
    return MediaStream(
        index=index,
        codec_type=_as_str(raw.get("codec_type")) or "unknown",
        codec_name=_as_str(raw.get("codec_name")),
        codec_long_name=_as_str(raw.get("codec_long_name")),
        profile=_as_str(raw.get("profile")),
        width=_as_int(raw.get("width")),
        height=_as_int(raw.get("height")),
        frame_rate=_as_str(raw.get("avg_frame_rate")) or _as_str(raw.get("r_frame_rate")),
        pixel_format=_as_str(raw.get("pix_fmt")),
        sample_aspect_ratio=_as_str(raw.get("sample_aspect_ratio")),
        display_aspect_ratio=_as_str(raw.get("display_aspect_ratio")),
        sample_rate=_as_int(raw.get("sample_rate")),
        channels=_as_int(raw.get("channels")),
        channel_layout=_as_str(raw.get("channel_layout")),
        bit_rate=_as_int(raw.get("bit_rate")),
    )


def parse_probe_output(stdout: str) -> MediaMetadata:
    """把 ffprobe 的 stdout 解析为 `MediaMetadata`。

    空输出、非 JSON、缺少 format 与 streams 都视为解析失败：这几种情况说明 ffprobe 没有
    真正分析出媒体信息，继续下去只会得到一份看似成功实则无用的元数据。
    """
    text = (stdout or "").strip()
    if not text:
        raise ProbeParseError("ffprobe 未输出任何内容，无法解析媒体信息")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeParseError(f"ffprobe 输出不是合法 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise ProbeParseError("ffprobe 输出的 JSON 顶层不是对象")

    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, list) or not raw_streams:
        raise ProbeParseError("ffprobe 未识别出任何媒体流，文件可能损坏或不是媒体文件")

    # 保留下标与原始条目的对应关系：duration 回退需要回到原始流里读包计数
    pairs = [
        (index, raw)
        for index, raw in enumerate(raw_streams)
        if isinstance(raw, dict)
    ]
    if not pairs:
        raise ProbeParseError("ffprobe 输出的媒体流结构异常，无法解析")
    streams = tuple(_parse_stream(raw, index) for index, raw in pairs)

    raw_format = payload.get("format") if isinstance(payload.get("format"), dict) else {}

    duration = _as_float(raw_format.get("duration"))
    if duration is None:
        # 容器头无 duration（典型是 TS 录屏）时回退到「包计数 ÷ 帧率」
        for stream, (_, raw) in zip(streams, pairs):
            if stream.codec_type == "video":
                duration = _fallback_duration_seconds(raw, stream.frame_rate)
                break

    return MediaMetadata(
        format_name=_as_str(raw_format.get("format_name")),
        format_long_name=_as_str(raw_format.get("format_long_name")),
        duration_seconds=duration,
        size_bytes=_as_int(raw_format.get("size")),
        bit_rate=_as_int(raw_format.get("bit_rate")),
        probe_score=_as_int(raw_format.get("probe_score")),
        stream_count=_as_int(raw_format.get("nb_streams")) or len(streams),
        streams=streams,
        raw_json=text,
    )


def has_playable_streams(metadata: MediaMetadata) -> bool:
    """至少要有音频或视频流，才算探测到了可用的媒体内容。"""
    return metadata.video is not None or metadata.audio is not None
