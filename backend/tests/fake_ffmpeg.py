"""转封装与切分用假 ffmpeg：以 Python 脚本实现，任何平台都能直接执行。

放在 tests 目录（而非测试用例内）是为了让 `Settings.ffmpeg_path` 指向一个固定路径，
同时脚本可把每次调用记录下来，供测试断言「调用参数对不对」「有没有多余调用」。

行为约定：
- 解析 `-i <源>` 与输出文件（最后一个参数），生成一段「与请求时长成正比」的假内容，
  使测试可以通过参数控制产物体积（`LIVERREVIEW_FAKE_FFMPEG_BYTES_PER_SECOND`）。
- 请求时长由输入输出的 `-ss` / `-to` 推得：转封装时从源文件读取（源文件里写有原始时长，
  见 `fake_ffprobe` 的约定），切片时由 `-ss` 与 `-to` 之差决定。
- 设置 `LIVERREVIEW_FAKE_FFMPEG_MODE` 改变行为：
  - `fail`：以退出码 1 失败，stderr 输出可识别的错误文本；
  - `timeout`：睡眠 30 秒，用于触发超时；
  - `empty`：退出码 0 但不写输出文件，验证「退出码为 0 却无产物」被判失败；
  - `shape`：读环境变量 `LIVERREVIEW_FAKE_FFMPEG_SHAPE` 决定产物在探测侧的形态
    （例如 `no-audio` 让产物被判定为丢了声音）。
- 设置 `LIVERREVIEW_FAKE_FFMPEG_LOG` 时，把每次调用的参数逐行追加写入该文件（JSON 行）。
- 产物内容本身不是真实媒体：探测由 `fake_ffprobe` 按文件旁边的 sidecar 元数据回答，
  因此测试关注的是编排（调用参数、产物校验、上传次序），而不是编解码本身。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 默认码率：16KiB/s，使「时长 → 体积」的换算在测试里可心算，又不会为 600 秒素材
# 真的写出百兆级文件（真实码率由测试按需通过环境变量放大，用于覆盖体积约束）。
DEFAULT_BYTES_PER_SECOND = 16 * 1024

# 源文件未被切片时（转封装）的时长来源：源文件旁的 sidecar 写着原始时长，
# 缺失时回落到该默认值（与 fake_ffprobe 的默认时长一致）。
DEFAULT_SOURCE_DURATION = 600.0

SIDECAR_SUFFIX = ".fakemeta.json"


def _parse_args(args: list[str]) -> dict:
    info: dict = {"args": args, "source": None, "output": None, "ss": None, "to": None}
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-i" and index + 1 < len(args):
            info["source"] = args[index + 1]
            index += 2
            continue
        if token == "-ss" and index + 1 < len(args):
            info["ss"] = float(args[index + 1])
            index += 2
            continue
        if token == "-to" and index + 1 < len(args):
            info["to"] = float(args[index + 1])
            index += 2
            continue
        index += 1
    # 输出文件是最后一个不以 "-" 开头的参数
    for token in reversed(args):
        if not token.startswith("-"):
            info["output"] = token
            break
    return info


def _source_duration(source: str | None) -> float:
    """转封装时的产物时长：取自源文件旁的 sidecar，缺失则用默认值。"""
    if not source:
        return DEFAULT_SOURCE_DURATION
    sidecar = Path(f"{source}{SIDECAR_SUFFIX}")
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return DEFAULT_SOURCE_DURATION
        duration = payload.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration > 0:
            return float(duration)
    return DEFAULT_SOURCE_DURATION


def _product_duration(info: dict) -> float:
    """产物时长：切片看 -ss/-to 之差，转封装看源文件的时长。"""
    if info["ss"] is not None and info["to"] is not None:
        return max(0.1, float(info["to"]) - float(info["ss"]))
    return _source_duration(info["source"])


def _write_sidecar(output: str, duration: float) -> None:
    """给产物写 sidecar：让 fake_ffprobe 能回答「这段产物有多长、有没有音频」。"""
    shape = os.environ.get("LIVERREVIEW_FAKE_FFMPEG_SHAPE", "normal")
    payload: dict = {
        "duration_seconds": duration,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "has_video": True,
        "has_audio": shape != "no-audio",
    }
    if shape == "truncated":
        # 产物时长明显短于请求时长：用于验证「转封装产物被截断」的判定
        payload["duration_seconds"] = duration * 0.5
    Path(f"{output}{SIDECAR_SUFFIX}").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def main() -> int:
    args = sys.argv[1:]
    info = _parse_args(args)

    log_path = os.environ.get("LIVERREVIEW_FAKE_FFMPEG_LOG")
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(info, ensure_ascii=False) + "\n")

    mode = os.environ.get("LIVERREVIEW_FAKE_FFMPEG_MODE", "")

    if mode == "timeout":
        time.sleep(30)
    if mode == "fail":
        sys.stderr.write("Invalid data found when processing input\n")
        return 1
    if mode == "empty":
        return 0

    output = info["output"]
    if not output:
        sys.stderr.write("No output file specified\n")
        return 1

    duration = _product_duration(info)
    bytes_per_second = int(
        os.environ.get("LIVERREVIEW_FAKE_FFMPEG_BYTES_PER_SECOND", DEFAULT_BYTES_PER_SECOND)
    )
    size = max(1, int(duration * bytes_per_second))

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(b"\0" * size)

    _write_sidecar(output, duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
