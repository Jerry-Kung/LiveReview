"""媒体探测模块：ffprobe 调用、输出解析与本地产物路径约定。

业务代码（任务体）从这里取 `probe_file()` 与 `ProbeError`，不直接接触子进程细节。
"""

from app.media.ffprobe import (
    MediaMetadata,
    MediaStream,
    ProbeParseError,
    has_playable_streams,
    parse_probe_output,
)
from app.media.paths import original_path, originals_dir, remove_original, sha256_file
from app.media.probe import ProbeError, build_ffprobe_args, probe_file

__all__ = [
    "MediaMetadata",
    "MediaStream",
    "ProbeError",
    "ProbeParseError",
    "build_ffprobe_args",
    "has_playable_streams",
    "original_path",
    "originals_dir",
    "parse_probe_output",
    "probe_file",
    "remove_original",
    "sha256_file",
]
