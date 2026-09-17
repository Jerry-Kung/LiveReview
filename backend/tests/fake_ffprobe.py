"""探测用假 ffprobe：以 Python 脚本实现，任何平台都能直接执行。

放在 tests 目录（而非测试用例内）是为了让 `Settings.ffprobe_path` 指向一个固定路径，
同时脚本自身可在被调用时写 side 文件，供测试断言「探测时用的是哪份文件」。

行为约定：
- 默认输出一份固定的 TS 元数据（有音视频流，时长 600 秒）。
- 设置 `LIVERREVIEW_FAKE_PROBE_MODE` 改变行为：
  - `bad-media`：以退出码 1 失败，stderr 输出可识别的错误文本；
  - `no-streams`：输出没有音视频流的 JSON，触发「不可播放」判定；
  - `slow`：睡眠 30 秒，用于触发超时。
  用环境变量而不是「文件名标记」控制行为，是因为本地副本按任务 id 命名，原始文件名
  不会出现在副本路径里。
- 设置 `LIVERREVIEW_FAKE_PROBE_LOG` 环境变量时，把收到的最后一个参数（媒体文件路径）
  追加写入该文件，供测试确认探测对象是本地副本。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

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

    payload = NO_STREAM_METADATA if mode == "no-streams" else METADATA
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
