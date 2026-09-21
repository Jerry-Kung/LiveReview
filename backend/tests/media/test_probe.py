"""ffprobe 调用的测试：参数构造、错误映射、超时与可执行文件缺失。

真实 ffprobe 只在测试环境存在，因此这里用「假 ffprobe 脚本」覆盖子进程调用的行为：
本机 Windows 无法直接执行脚本，需要真实执行的用例以 POSIX 为条件跳过，参数构造与
缺失可执行文件两类用例在任何平台都能跑。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from app.media.probe import ProbeError, build_ffprobe_args, probe_file

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="假 ffprobe 脚本只能在 POSIX 上直接执行"
)

SAMPLE_STDOUT = json.dumps(
    {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30/1",
            }
        ],
        "format": {"format_name": "mpegts", "duration": "42.0", "size": "1024", "nb_streams": 1},
    }
)


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    target = tmp_path / "sample.ts"
    target.write_bytes(b"\x00" * 2048)
    return target


def test_args_pass_source_without_shell_and_request_packet_count():
    args = build_ffprobe_args("ffprobe", "/media/originals/abc.ts")

    assert args[0] == "ffprobe"
    # -count_packets 是 TS 时长回退的依据，不能被顺手删掉
    assert "-count_packets" in args
    assert "-show_streams" in args
    assert "-show_format" in args
    assert args[-1] == "/media/originals/abc.ts"
    assert isinstance(args, list)


def test_missing_source_file_raises_probe_error(tmp_path: Path):
    with pytest.raises(ProbeError) as excinfo:
        probe_file(tmp_path / "absent.ts")

    assert "不存在" in str(excinfo.value)


def test_missing_ffprobe_binary_is_reported_clearly(media_file: Path):
    with pytest.raises(ProbeError) as excinfo:
        probe_file(media_file, ffprobe_path="ffprobe-not-installed-anywhere")

    message = str(excinfo.value)
    assert "未找到 ffprobe" in message
    # 提示要指向可行的做法：本地不部署，去测试环境或显式指定路径
    assert "FFPROBE_PATH" in message


@POSIX_ONLY
def test_successful_probe_returns_parsed_metadata(tmp_path: Path, media_file: Path):
    script = _write_script(tmp_path / "fake-ffprobe", f"#!/bin/sh\ncat <<'JSON'\n{SAMPLE_STDOUT}\nJSON\n")

    metadata = probe_file(media_file, ffprobe_path=str(script))

    assert metadata.format_name == "mpegts"
    assert metadata.duration_seconds == pytest.approx(42.0)
    assert metadata.video is not None
    assert metadata.video.width == 1280


@POSIX_ONLY
def test_nonzero_exit_code_carries_stderr_excerpt(tmp_path: Path, media_file: Path):
    script = _write_script(
        tmp_path / "fake-ffprobe",
        "#!/bin/sh\necho 'Invalid data found when processing input' >&2\nexit 1\n",
    )

    with pytest.raises(ProbeError) as excinfo:
        probe_file(media_file, ffprobe_path=str(script))

    message = str(excinfo.value)
    assert "退出码 1" in message
    assert "Invalid data found" in message


@POSIX_ONLY
def test_stderr_excerpt_is_truncated_to_keep_failure_reason_storable(tmp_path: Path, media_file: Path):
    script = _write_script(
        tmp_path / "fake-ffprobe",
        "#!/bin/sh\n"
        "i=0\n"
        "while [ $i -lt 200 ]; do echo 'noise-noise-noise-noise-noise' >&2; i=$((i+1)); done\n"
        "exit 2\n",
    )

    with pytest.raises(ProbeError) as excinfo:
        probe_file(media_file, ffprobe_path=str(script))

    message = str(excinfo.value)
    assert len(message) < 800
    assert "…" in message


@POSIX_ONLY
def test_timeout_is_mapped_to_probe_error(tmp_path: Path, media_file: Path):
    script = _write_script(tmp_path / "fake-ffprobe", "#!/bin/sh\nsleep 5\n")

    with pytest.raises(ProbeError) as excinfo:
        probe_file(media_file, ffprobe_path=str(script), timeout_seconds=1)

    assert "超时" in str(excinfo.value)


@POSIX_ONLY
def test_unparsable_output_is_mapped_to_probe_error(tmp_path: Path, media_file: Path):
    script = _write_script(tmp_path / "fake-ffprobe", "#!/bin/sh\necho 'garbage'\n")

    with pytest.raises(ProbeError) as excinfo:
        probe_file(media_file, ffprobe_path=str(script))

    assert "JSON" in str(excinfo.value)


@POSIX_ONLY
def test_oversized_output_is_rejected(tmp_path: Path, media_file: Path):
    script = _write_script(
        tmp_path / "fake-ffprobe",
        "#!/bin/sh\nhead -c 4096 /dev/zero | tr '\\0' 'a'\n",
    )

    with pytest.raises(ProbeError) as excinfo:
        probe_file(media_file, ffprobe_path=str(script), max_output_bytes=512)

    assert "上限" in str(excinfo.value)


@POSIX_ONLY
def test_output_without_playable_streams_is_rejected(tmp_path: Path, media_file: Path):
    payload = json.dumps(
        {"streams": [{"index": 0, "codec_type": "data"}], "format": {"format_name": "mpegts"}}
    )
    script = _write_script(tmp_path / "fake-ffprobe", f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\n")

    with pytest.raises(ProbeError) as excinfo:
        probe_file(media_file, ffprobe_path=str(script))

    assert "没有音频或视频流" in str(excinfo.value)


@POSIX_ONLY
def test_probe_passes_the_file_path_as_last_argument(tmp_path: Path, media_file: Path):
    """假 ffprobe 把收到的参数的最后一个写进 side 文件，确认传参顺序正确。"""
    argv_log = tmp_path / "argv.txt"
    script = _write_script(
        tmp_path / "fake-ffprobe",
        f'#!/bin/sh\nfor last in "$@"; do :; done\nprintf "%s" "$last" > "{argv_log}"\n'
        'printf \'{"streams":[{"index":0,"codec_type":"video","codec_name":"h264"}],'
        '"format":{"format_name":"mpegts","duration":"1.0"}}\'\n',
    )

    probe_file(media_file, ffprobe_path=str(script))

    assert argv_log.read_text(encoding="utf-8") == str(media_file)
