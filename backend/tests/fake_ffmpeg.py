"""转封装与切分用假 ffmpeg：以 Python 脚本实现，任何平台都能直接执行。

放在 tests 目录（而非测试用例内）是为了让 `Settings.ffmpeg_path` 指向一个固定路径，
同时脚本可把每次调用记录下来，供测试断言「调用参数对不对」「有没有多余调用」。

行为约定：
- 解析 `-i <源>` 与输出文件（最后一个参数），生成一段「与请求时长成正比」的假内容，
  使测试可以通过参数控制产物体积（`LIVERREVIEW_FAKE_FFMPEG_BYTES_PER_SECOND`）。
- 请求时长由输入输出的 `-ss` / `-t` / `-to` 推得：转封装时从源文件读取（源文件里写有原始
  时长，见 `fake_ffprobe` 的约定），切片时由 `-t` 决定；`-to` 仍按输入时间轴的绝对时刻
  参与计算，以便替身能如实暴露「用 `-to` 限定切片长度」这类误用。
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

# 本项目用到的 ffmpeg 选项按位置分类。这个分类存在的意义就是「位置错就报错」：
# 替身若对参数顺序照单全收，`-map` 写到 `-i` 之前这种错误在本地测试里就查不出来，
# 只能等真实 ffmpeg 在测试环境报错（V0.1.5 正是这样暴露的）。
GLOBAL_OPTIONS = frozenset({"-hide_banner", "-nostdin", "-v", "-loglevel", "-y", "-n"})
# `-ss`、`-t`、`-to` 两侧都合法（含义不同），因此不参与位置校验：替身只拦真正会失败的错位
INPUT_OPTIONS = frozenset({"-fflags", "-f", "-probesize", "-analyzeduration"})
OUTPUT_OPTIONS = frozenset({"-map", "-c", "-codec", "-movflags", "-avoid_negative_ts", "-an", "-vn"})
# 带参数值的选项：解析时需连同其取值一起跳过
OPTIONS_WITH_VALUE = GLOBAL_OPTIONS | INPUT_OPTIONS | OUTPUT_OPTIONS | {"-ss", "-t", "-to"}


def _validate_option_placement(args: list[str]) -> str | None:
    """按位置校验选项，返回错误文本（参数合法时返回 None）。

    与真实 ffmpeg 一致：选项只作用于紧随其后的那个文件，输入选项出现在 `-i` 之后、
    输出选项出现在 `-i` 之前都会被拒绝执行。
    """
    seen_input = False
    for token in args:
        if token == "-i":
            seen_input = True
            continue
        if token not in OPTIONS_WITH_VALUE:
            continue
        if token in OUTPUT_OPTIONS and not seen_input:
            return (
                f"Option {token.lstrip('-')} cannot be applied to input url {args[-1]} -- "
                "you are trying to apply an input option to an output file or vice versa"
            )
        if token in INPUT_OPTIONS and seen_input:
            return (
                f"Option {token.lstrip('-')} (set input option) cannot be applied to output file "
                f"{args[-1]}: move this option before the file it belongs to"
            )
    return None


def _parse_args(args: list[str]) -> dict:
    info: dict = {
        "args": args,
        "source": None,
        "output": None,
        "ss": None,
        "t": None,
        "to": None,
    }
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
        if token == "-t" and index + 1 < len(args):
            info["t"] = float(args[index + 1])
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
    """产物时长：切片看 `-t`（`-to` 按输入时间轴绝对时刻参与比较），转封装看源文件时长。

    `-to` 的语义与真实 ffmpeg 一致：它是**输入时间轴上的绝对时刻**，不随输入侧 `-ss`
    平移。因此只写 `-to`、不写 `-t` 时，产出的长度是「end - 实际起点」，越靠后的片段
    超出越多——替身如实算出这个长度，这类误用才可能被本地测试发现。
    """
    if info["t"] is not None:
        return max(0.1, float(info["t"]))
    if info["to"] is not None:
        return max(0.1, float(info["to"]) - float(info["ss"] or 0.0))
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

    placement_error = _validate_option_placement(args)
    if placement_error:
        # 与真实 ffmpeg 一致：参数位置错误时以非零码失败并把原因写到 stderr
        sys.stderr.write(f"{placement_error}\n")
        return 1

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
