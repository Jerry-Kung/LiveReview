"""探测用假 ffprobe：以 Python 脚本实现，任何平台都能直接执行。

放在 tests 目录（而非测试用例内）是为了让 `Settings.ffprobe_path` 指向一个固定路径，
同时脚本自身可在被调用时写 side 文件，供测试断言「探测时用的是哪份文件」。

行为约定：
- 若被探测文件旁边存在 `<文件>.fakemeta.json`（由 `fake_ffmpeg` 写出），则按该 sidecar
  回答：转封装产物与切片产物的时长、容器、有无音频都从那里读；原始素材没有 sidecar，
  走下面的固定元数据。
- 默认输出一份固定的 TS 元数据（有音视频流，时长 600 秒）。
- 请求 `-show_entries packet=pts_time` 时改走「逐包清单」模式：按下面两条环境变量造一份
  `流下标:时间戳` 的清单，用于覆盖时间戳断层统计（转封装产物偏短是否可解释）。
- 设置 `LIVERREVIEW_FAKE_PROBE_MODE` 改变行为：
  - `bad-media`：以退出码 1 失败，stderr 输出可识别的错误文本；
  - `no-streams`：输出没有音视频流的 JSON，触发「不可播放」判定；
  - `packets-fail`：逐包清单模式以退出码 1 失败（用于覆盖「量不到断层」的分支）；
  - `slow`：睡眠 30 秒，用于触发超时。
  用环境变量而不是「文件名标记」控制行为，是因为本地副本按任务 id 命名，原始文件名
  不会出现在副本路径里。
- 设置 `LIVERREVIEW_FAKE_PROBE_LOG` 环境变量时，把收到的最后一个参数（媒体文件路径）
  追加写入该文件，供测试确认探测对象是本地副本。

逐包清单模式的环境变量：
- `LIVERREVIEW_FAKE_PROBE_PACKET_COUNT`：包数（默认 100）；
- `LIVERREVIEW_FAKE_PROBE_PACKET_INTERVAL`：正常包间隔秒数（默认 0.04）；
- `LIVERREVIEW_FAKE_PROBE_PACKET_GAP_AT`：在第几个包之后插入断层（默认不插）；
- `LIVERREVIEW_FAKE_PROBE_PACKET_GAP_SECONDS`：断层秒数（默认 10）。

清单以 `流下标:时间戳` 逐行输出，由假脚本按需插入断层，从而在本地就能覆盖「源素材有
时间戳空洞 → 产物时长合法变短」的完整链路，无需真实 TS 素材。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SIDECAR_SUFFIX = ".fakemeta.json"

# 逐包清单的默认形态：100 个包、间隔 40ms，即一段 4 秒的连续时间轴
DEFAULT_PACKET_COUNT = 100
DEFAULT_PACKET_INTERVAL = 0.04
DEFAULT_PACKET_GAP_SECONDS = 10.0
# sidecar 缺失时的产物时长，与 `fake_ffmpeg` 的 DEFAULT_SOURCE_DURATION 保持一致
DEFAULT_PACKET_DURATION = 600.0

METADATA = {
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "codec_long_name": "H.264 / AVC / MPEG-4 AVC",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "25/1",
            "pix_fmt": "yuv420p",
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
        },
    ],
    "format": {
        "format_name": "mpegts",
        "format_long_name": "MPEG-TS (MPEG-2 Transport Stream)",
        "duration": "600.0",
        "size": "3145728",
        "bit_rate": "3500000",
        "probe_score": 50,
        "nb_streams": 2,
    },
}

NO_STREAM_METADATA = {
    "streams": [{"index": 0, "codec_type": "data", "codec_name": "bin_data"}],
    "format": {"format_name": "mpegts", "duration": "10.0", "nb_streams": 1},
}


def _video_stream() -> dict:
    return json.loads(json.dumps(METADATA["streams"][0]))


def _audio_stream() -> dict:
    return json.loads(json.dumps(METADATA["streams"][1]))


def _sidecar_metadata(source: Path) -> dict | None:
    """按 fake_ffmpeg 写出的 sidecar 构造探测结果；无 sidecar 时返回 None。"""
    sidecar = Path(f"{source}{SIDECAR_SUFFIX}")
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    streams: list[dict] = []
    if payload.get("has_video", True):
        streams.append(_video_stream())
    if payload.get("has_audio", True):
        streams.append(_audio_stream())

    duration = payload.get("duration_seconds", 600.0)
    size = source.stat().st_size if source.is_file() else 0
    return {
        "streams": streams,
        "format": {
            "format_name": payload.get("format_name", "mov,mp4,m4a,3gp,3g2,mj2"),
            "format_long_name": "QuickTime / MOV",
            "duration": str(duration),
            "size": str(size),
            "probe_score": 50,
            "nb_streams": len(streams),
        },
    }


def _write_packet_listing(stream_index: int, source: Path | None) -> None:
    """输出逐包清单：`流下标:时间戳`，与 ffprobe 的 `-print_format csv=p=0` 一致。

    源与产物的形态不同，替身照实体现：

    - TS 源（无 sidecar）时间轴连续、按 `LIVERREVIEW_FAKE_PROBE_PACKET_*` 可插入断层，
      逐包跨度与自己的标称时长一致（真实源正是几百秒的录屏）；
    - 转封装/切片产物（有 sidecar）同样按 sidecar 时长铺开，产物真的短了则逐包统计也跟着
      短——否则「按可播放时长比对」这条判定在本地就形同虚设。
    """
    if source is not None and _sidecar_metadata(source) is None:
        timestamps = _packet_timestamps(spread_to_duration=DEFAULT_PACKET_DURATION)
    else:
        # 包数按 sidecar 写明的产物时长缩放
        ratio = _env_number("LIVERREVIEW_FAKE_PROBE_PRODUCT_PACKET_RATIO", 1.0)
        timestamps = _packet_timestamps(
            include_gap=False,
            spread_to_duration=_sidecar_duration(source) * ratio,
        )
    for timestamp in timestamps:
        sys.stdout.write(f"{stream_index}:{timestamp:.6f}\n")


def _sidecar_duration(source: Path | None) -> float:
    """sidecar 里写的产物时长；读不到时用默认时长，与 `_sidecar_metadata` 的回落一致。"""
    if source is None:
        return DEFAULT_PACKET_DURATION
    sidecar = Path(f"{source}{SIDECAR_SUFFIX}")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return DEFAULT_PACKET_DURATION
    duration = payload.get("duration_seconds")
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    return DEFAULT_PACKET_DURATION


def _packet_timestamps(
    include_gap: bool = True,
    count_ratio: float = 1.0,
    spread_to_duration: float | None = None,
) -> list[float]:
    """造一份逐包清单的时间戳：等间隔排列，可在指定位置插入一处断层。

    `include_gap=False` 时按「断层被抹平」生成：正常包间隔不变，但不插入那段空洞，
    因此后续包的时间戳整体前移——与真实转封装产物一致。

    `spread_to_duration=True` 时把包数调成「覆盖本文件的标称时长、且包间隔保持默认」：
    产物的逐包跨度应与它自己的标称时长相符，同时包间隔仍在正常量级。

    设置 `LIVERREVIEW_FAKE_PROBE_PACKET_OUT_OF_ORDER` 时每隔一个包回退半个间隔，
    复刻真实素材里 B 帧导致的 PTS 乱序。
    """
    count = _env_number("LIVERREVIEW_FAKE_PROBE_PACKET_COUNT", DEFAULT_PACKET_COUNT)
    interval = _env_number("LIVERREVIEW_FAKE_PROBE_PACKET_INTERVAL", DEFAULT_PACKET_INTERVAL)
    gap_at = os.environ.get("LIVERREVIEW_FAKE_PROBE_PACKET_GAP_AT", "").strip()
    gap_seconds = _env_number("LIVERREVIEW_FAKE_PROBE_PACKET_GAP_SECONDS", DEFAULT_PACKET_GAP_SECONDS)
    out_of_order = bool(os.environ.get("LIVERREVIEW_FAKE_PROBE_PACKET_OUT_OF_ORDER", "").strip())

    total = max(1, int(max(1.0, count * count_ratio)))
    if spread_to_duration is not None:
        # 按真实包间隔铺满整段时长：清单的包间隔必须与真实录屏同量级，否则每一处正常
        # 间隔都会被断层阈值判成空洞（22fps 与 25fps 素材的包间隔都在数十毫秒）
        total = max(1, int(spread_to_duration / interval))

    timestamps: list[float] = []
    current = 0.0
    for index in range(total):
        if out_of_order and index % 2 == 1:
            timestamps.append(current - interval / 2)
        else:
            timestamps.append(current)
        current += interval
        if include_gap and gap_at and index + 1 == int(float(gap_at)):
            current += gap_seconds
    return timestamps


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def main() -> int:
    args = sys.argv[1:]
    source = Path(args[-1]) if args else None

    log_path = os.environ.get("LIVERREVIEW_FAKE_PROBE_LOG")
    if log_path and source is not None:
        with Path(log_path).open("a", encoding="utf-8") as handle:
            handle.write(f"{source}\n")

    mode = os.environ.get("LIVERREVIEW_FAKE_PROBE_MODE", "")

    if mode == "slow":
        time.sleep(30)
    if mode == "bad-media":
        sys.stderr.write("Invalid data found when processing input\n")
        return 1

    if "packet=pts_time" in args:
        if mode == "packets-fail":
            sys.stderr.write("Failed to read packets\n")
            return 1
        _write_packet_listing(_selected_stream_index(args), source)
        return 0

    payload = NO_STREAM_METADATA if mode == "no-streams" else METADATA
    if mode != "no-streams" and source is not None:
        payload = _sidecar_metadata(source) or payload

    sys.stdout.write(json.dumps(payload))
    return 0


def _selected_stream_index(args: list[str]) -> int:
    """从 `-select_streams` 里取流下标。

    ffprobe 用**流在文件中的实际下标**标注包，本项目两份素材都是 0=视频、1=音频，
    替身照此回答；给不出下标时按 0 处理。
    """
    if "-select_streams" not in args:
        return 0
    position = args.index("-select_streams") + 1
    if position >= len(args):
        return 0
    selector = args[position]
    try:
        _, _, index = selector.partition(":")
        return int(index)
    except ValueError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
