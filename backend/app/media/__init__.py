"""媒体处理模块：探测、转封装、切分、覆盖校验与本地产物路径约定。

业务代码（任务体）从这里取 `probe_file()` / `prepare_split_source()` / `plan_clips()` 等
能力，不直接接触子进程细节与路径拼装。
"""

from app.media.convert import (
    convert_to_mp4,
    duration_tolerance,
    is_target_container,
    prepare_split_source,
    validate_converted,
)
from app.media.coverage import (
    CoverageIssue,
    check_coverage,
    issues_from_json,
    issues_to_json,
    summarize,
)
from app.media.ffmpeg import FFmpegError, build_ffmpeg_args, run_ffmpeg
from app.media.ffprobe import (
    MediaMetadata,
    MediaStream,
    ProbeParseError,
    has_playable_streams,
    parse_probe_output,
)
from app.media.paths import (
    clip_filename,
    clip_path,
    clips_dir,
    converted_path,
    original_path,
    originals_dir,
    remove_clip,
    remove_clips_tree,
    remove_converted,
    remove_file,
    remove_original,
    remove_task_media,
    sha256_file,
)
from app.media.probe import (
    DISCONTINUITY_THRESHOLD_SECONDS,
    ProbeError,
    TimelineReport,
    build_ffprobe_args,
    measure_timeline,
    probe_file,
)
from app.media.split import (
    ClipPlan,
    SplitError,
    cut_clip,
    plan_clips,
    plan_initial_clips,
    refine_by_size,
    validate_clip,
)

__all__ = [
    "ClipPlan",
    "CoverageIssue",
    "DISCONTINUITY_THRESHOLD_SECONDS",
    "FFmpegError",
    "MediaMetadata",
    "MediaStream",
    "ProbeError",
    "ProbeParseError",
    "SplitError",
    "TimelineReport",
    "build_ffmpeg_args",
    "build_ffprobe_args",
    "check_coverage",
    "clip_filename",
    "clip_path",
    "clips_dir",
    "convert_to_mp4",
    "converted_path",
    "cut_clip",
    "duration_tolerance",
    "has_playable_streams",
    "is_target_container",
    "issues_from_json",
    "issues_to_json",
    "measure_timeline",
    "original_path",    "originals_dir",
    "parse_probe_output",
    "plan_clips",
    "plan_initial_clips",
    "prepare_split_source",
    "probe_file",
    "refine_by_size",
    "remove_clip",
    "remove_clips_tree",
    "remove_converted",
    "remove_file",
    "remove_original",
    "remove_task_media",
    "run_ffmpeg",
    "sha256_file",
    "summarize",
    "validate_clip",
    "validate_converted",
]
