"""ffmpeg 调用层与转封装/切片执行的测试。

本机不部署 FFmpeg，因此用 `tests/fake_ffmpeg.py` 替代真实工具：这里覆盖的是「参数构造是否
正确」「产物校验是否生效」「错误映射是否明确」，而不是编解码本身。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.media.convert import (
    REMUX_ARGS,
    REMUX_INPUT_ARGS,
    convert_to_mp4,
    duration_tolerance,
    is_target_container,
    prepare_split_source,
    validate_converted,
)
from app.media.ffmpeg import FFmpegError, build_ffmpeg_args, run_ffmpeg
from app.media.ffprobe import MediaMetadata, MediaStream
from app.media.probe import ProbeError, measure_timeline
from app.media.split import (
    CLIP_ARGS_TEMPLATE,
    CLIP_INPUT_ARGS_TEMPLATE,
    ClipPlan,
    SplitError,
    cut_clip,
    validate_clip,
)
from tests.conftest import fake_ffmpeg_args, fake_ffprobe_args
from tests.fake_ffmpeg import SIDECAR_SUFFIX

MiB = 1024 * 1024


def _video_stream() -> MediaStream:
    return MediaStream(index=0, codec_type="video", codec_name="h264", width=1920, height=1080)


def _audio_stream() -> MediaStream:
    return MediaStream(index=1, codec_type="audio", codec_name="aac", sample_rate=48000, channels=2)


def _metadata(
    duration: float = 600.0,
    *,
    video: bool = True,
    audio: bool = True,
    format_name: str = "mpegts",
) -> MediaMetadata:
    streams = []
    if video:
        streams.append(_video_stream())
    if audio:
        streams.append(_audio_stream())
    return MediaMetadata(
        format_name=format_name,
        duration_seconds=duration,
        stream_count=len(streams),
        streams=tuple(streams),
    )


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """一份假素材：内容不重要，长度与存在性才是探测与切分的输入。"""
    path = tmp_path / "live.ts"
    path.write_bytes(b"\0" * (64 * 1024))
    return path


# —— 参数构造 ——


def test_build_ffmpeg_args_puts_globals_first_and_io_last():
    args = build_ffmpeg_args("ffmpeg", source="in.ts", output="out.mp4")
    assert args[0] == "ffmpeg"
    assert "-nostdin" in args
    assert "-y" in args
    assert args.index("-i") < args.index("out.mp4")


def test_build_ffmpeg_args_places_input_args_before_input_file():
    """输入选项必须落在 `-i` 之前：ffmpeg 只把它作用于紧随其后的那个文件。"""
    args = build_ffmpeg_args(
        "ffmpeg",
        source="in.ts",
        output="out.mp4",
        input_args=("-fflags", "+genpts"),
        extra_args=("-c", "copy"),
    )
    assert args.index("+genpts") < args.index("-i")
    assert args.index("-i") < args.index("-c")


def test_build_ffmpeg_args_accepts_interpreter_plus_script():
    args = build_ffmpeg_args([sys.executable, "fake.py"], source="a", output="b")
    assert args[:2] == [sys.executable, "fake.py"]


def test_build_ffmpeg_args_rejects_empty_command():
    with pytest.raises(FFmpegError, match="命令为空"):
        build_ffmpeg_args("", source="a", output="b")


def test_remux_args_keep_all_audio_and_copy_without_reencoding():
    """转封装必须保留音频、不重编码：音频是复盘的核心信息，重编码会劣化画面。"""
    assert "-map" in REMUX_ARGS
    assert "0:v" in REMUX_ARGS
    assert "0:a?" in REMUX_ARGS
    assert REMUX_ARGS[REMUX_ARGS.index("-c") + 1] == "copy"
    assert "+faststart" in REMUX_ARGS
    # genpts 是解复用阶段的选项，必须在输入侧，否则 ffmpeg 直接拒绝执行
    assert "+genpts" in REMUX_INPUT_ARGS


def test_remux_put_audio_and_mapping_after_the_input_file():
    """回归用例：`-map` 曾是输入侧的选项，真实 ffmpeg 报
    「Option map cannot be applied to input url」并让整个任务失败。"""
    args = build_ffmpeg_args(
        "ffmpeg",
        source="live.ts",
        output="converted.mp4",
        input_args=REMUX_INPUT_ARGS,
        extra_args=REMUX_ARGS,
    )
    assert args.index("-i") < args.index("-map")
    assert args.index("-i") < args.index("-c")
    assert args.index("-i") < args.index("-movflags")
    # 输入侧只留 genpts 这类解复用选项
    assert args.index("+genpts") < args.index("-i")


def test_clip_args_cut_by_time_and_keep_audio():
    args = ClipPlan(index=2, start_seconds=120.5, end_seconds=300.0).to_args()
    assert args[args.index("-t") + 1] == "179.500"
    # 保留全部音视频流，且不重编码
    assert "0:v" in args and "0:a?" in args
    assert args[args.index("-c") + 1] == "copy"
    assert CLIP_ARGS_TEMPLATE.count("{start}") == 0


def test_clip_length_is_given_as_duration_not_end_timestamp():
    """回归用例：输出侧的 `-to` 是输入时间轴上的绝对时刻，不随输入侧 `-ss` 平移。

    用它限定切片长度时，第 N 片（计划区间 [a, b)）会退化成「从 a 一直复制到第 b 秒」，
    越靠后的片段超出越多。测试环境据此复现：第 1 片计划 1351.6s、实际 2703.7s。
    """
    planned = ClipPlan(index=1, start_seconds=120.5, end_seconds=300.0)
    args = planned.to_args()

    # 输出侧只出现时长（179.500），既不出现结束时刻，也不出现起点
    assert "-to" not in args
    assert args[args.index("-t") + 1] == "179.500"
    assert "300.000" not in args
    assert "120.500" not in args


def test_clip_seek_goes_before_input_and_stop_after_input():
    """`-ss` 放输入侧是快速定位，时长与流映射属输出侧；错位会让切分直接失败。"""
    planned = ClipPlan(index=2, start_seconds=120.5, end_seconds=300.0)
    args = build_ffmpeg_args(
        "ffmpeg",
        source="converted.mp4",
        output="clip_002.mp4",
        input_args=planned.to_input_args(),
        extra_args=planned.to_args(),
    )
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-ss") + 1] == "120.500"
    assert args.index("-i") < args.index("-t")
    assert args[args.index("-t") + 1] == "179.500"
    assert CLIP_INPUT_ARGS_TEMPLATE.count("{duration}") == 0
    assert CLIP_ARGS_TEMPLATE.count("{start}") == 0


# —— 容器判定 ——


@pytest.mark.parametrize("format_name", ["mov,mp4,m4a,3gp,3g2,mj2", "mp4", "MP4", "mov"])
def test_mp4_family_containers_are_recognised(format_name):
    assert is_target_container(format_name) is True


@pytest.mark.parametrize("format_name", ["mpegts", "matroska,webm", "flv", "avi", None])
def test_other_containers_need_conversion(format_name):
    assert is_target_container(format_name) is False


# —— 产物校验 ——


def test_validate_converted_accepts_matching_product():
    validate_converted(_metadata(600.0), _metadata(600.2, format_name="mov,mp4,m4a,3gp,3g2,mj2"))


def test_validate_converted_rejects_truncated_product():
    with pytest.raises(FFmpegError, match="时长与原始视频偏差过大"):
        validate_converted(_metadata(600.0), _metadata(300.0, format_name="mp4"))


def test_validate_converted_rejects_lost_audio():
    """丢音频一律判失败：无声片段对录屏复盘没有价值。"""
    with pytest.raises(FFmpegError, match="丢失了声音"):
        validate_converted(_metadata(600.0), _metadata(600.0, audio=False, format_name="mp4"))


def test_validate_converted_rejects_lost_video():
    with pytest.raises(FFmpegError, match="没有视频流"):
        validate_converted(_metadata(600.0), _metadata(600.0, video=False, format_name="mp4"))


def test_validate_converted_rejects_wrong_container():
    with pytest.raises(FFmpegError, match="容器仍不是 MP4"):
        validate_converted(_metadata(600.0), _metadata(600.0, format_name="matroska,webm"))


def test_validate_converted_rejects_unreadable_duration():
    """产物读不出时长说明它已经不可用，不能当成「时长未知」放过。"""
    broken = MediaMetadata(
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=None,
        stream_count=2,
        streams=(_video_stream(), _audio_stream()),
    )
    with pytest.raises(FFmpegError, match="无法读出时长"):
        validate_converted(_metadata(600.0), broken)


def test_validate_converted_skips_duration_check_when_source_duration_unknown():
    """原始视频时长未知时跳过时长比对：没有基准就不臆造结论。"""
    unknown_source = MediaMetadata(
        format_name="mpegts",
        duration_seconds=None,
        stream_count=2,
        streams=(_video_stream(), _audio_stream()),
    )
    validate_converted(unknown_source, _metadata(600.0, format_name="mp4"))


def test_duration_tolerance_has_an_absolute_floor():
    """短素材按比例算出的容差太小，取绝对下限。"""
    assert duration_tolerance(10.0) == pytest.approx(1.0)
    assert duration_tolerance(2000.0) == pytest.approx(10.0)


def test_validate_converted_compares_playable_time_not_nominal_duration():
    """回归用例：源的标称时长包含直播断流的空洞，不该拿它当比对基准。

    线上那份素材：源标称 1683.2s，产物 1649.7s，偏差 33.5s 远超 8.4s 容差，被判
    「产物可能被截断」，实际一个包都没少。改用可播放时长后两侧几乎相同（1638.1s 对
    1638.2s），不再有误判。
    """
    validate_converted(
        _metadata(1683.2),
        _metadata(1649.7, format_name="mov,mp4,m4a,3gp,3g2,mj2"),
        original_media_seconds=1638.1,
        converted_media_seconds=1638.2,
    )


def test_validate_converted_still_rejects_a_truncated_product_in_playable_time():
    """换成可播放时长也不是免死金牌：真的短了照样判失败。"""
    with pytest.raises(FFmpegError, match="时长与原始视频偏差过大"):
        validate_converted(
            _metadata(1683.2),
            _metadata(1649.7, format_name="mp4"),
            original_media_seconds=1638.1,
            converted_media_seconds=900.0,
        )


def test_validate_converted_falls_back_to_nominal_duration_when_unmeasured():
    """量不出可播放时长时退回标称比对，按最严容差判——没有证据就不给放宽。"""
    with pytest.raises(FFmpegError, match="时长与原始视频偏差过大"):
        validate_converted(
            _metadata(1683.2),
            _metadata(1649.7, format_name="mp4"),
            original_media_seconds=None,
            converted_media_seconds=None,
        )


def test_validate_converted_accepts_a_slightly_longer_product():
    """产物略长只可能来自首尾对齐，容差之内不该判失败。"""
    validate_converted(
        _metadata(600.0),
        _metadata(601.0, format_name="mp4"),
        original_media_seconds=600.0,
        converted_media_seconds=601.0,
    )


# —— 时间轴统计 ——


def test_measure_timeline_needs_a_real_file(tmp_path: Path):
    with pytest.raises(ProbeError, match="不存在或不是普通文件"):
        measure_timeline(tmp_path / "absent.ts", ffprobe_path=fake_ffprobe_args())


def test_measure_timeline_reports_playable_media_for_a_gapfree_source(source: Path):
    """时间轴连续时可播放时长就是首尾跨度，`measured` 为真。"""
    report = measure_timeline(source, ffprobe_path=fake_ffprobe_args())

    assert report.measured is True
    assert report.gap_count == 0
    # 替身的源清单按默认时长（600s）铺包，首尾跨度为 600s 减去一个包间隔
    assert report.media_seconds == pytest.approx(600.0 - 0.04, abs=0.1)


def test_measure_timeline_excludes_discontinuities_from_playable_time(source: Path, monkeypatch):
    """断层不计入可播放时长，这正是源与产物可比的依据。

    在第 2 个包之后插入 11s 空洞：首尾跨度随之变长，但那 11s 加一个正常包间隔被归为断层
    （量的是相邻两个包的时间戳之差），可播放时长只少掉一个正常包间隔。
    """
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_GAP_AT", "2")
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_GAP_SECONDS", "11.0")
    report = measure_timeline(source, ffprobe_path=fake_ffprobe_args())

    assert report.measured is True
    assert report.gap_count == 1
    assert report.gap_seconds == pytest.approx(11.04, abs=0.05)
    assert report.media_seconds == pytest.approx(600.0 - 0.04, abs=0.1)


def test_measure_timeline_ignores_normal_packet_spacing(source: Path, monkeypatch):
    """正常包间隔不算断层：22fps 的录屏每包约 45ms，累计起来会淹没真正的空洞。

    把包间隔压到 10ms 并数满 4000 个包：若实现按相邻包相减判断，这里会累计出上百秒的
    伪断层；按「超过阈值才算断层」则一处都没有。
    """
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_INTERVAL", "0.01")
    report = measure_timeline(source, ffprobe_path=fake_ffprobe_args())

    assert report.gap_count == 0
    assert report.gap_seconds == 0.0


def test_measure_timeline_ignores_out_of_order_timestamps(source: Path, monkeypatch):
    """B 帧让 PTS 乱序，回退的包不能被当成负断层，也不能把跨度算长。

    真实素材里近半数包是回退的（相邻差 -0.045s），按相邻包相减会凭空造出大量伪影。
    """
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_OUT_OF_ORDER", "1")
    ordered = measure_timeline(source, ffprobe_path=fake_ffprobe_args())

    assert ordered.gap_count == 0
    assert ordered.gap_seconds == 0.0
    assert ordered.media_seconds > 0


def test_measure_timeline_raises_when_ffprobe_fails(source: Path, monkeypatch):
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_MODE", "packets-fail")

    with pytest.raises(ProbeError, match="退出码 1"):
        measure_timeline(source, ffprobe_path=fake_ffprobe_args())


def test_measure_timeline_is_unmeasured_when_the_listing_is_truncated(source: Path, monkeypatch):
    """清单被截断时返回「没量到」而不是一个偏短的时长。

    偏短的源时长会让产物看上去「没有变短」，真实的截断就被放过去了。
    """
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_COUNT", "200")
    report = measure_timeline(source, ffprobe_path=fake_ffprobe_args(), max_scan_bytes=64)

    assert report.measured is False
    assert report.media_seconds == 0.0


def test_measure_timeline_rejects_missing_binary(source: Path):
    with pytest.raises(ProbeError, match="未找到 ffprobe"):
        measure_timeline(source, ffprobe_path="liverreview-no-such-ffprobe")


# —— 调用执行 ——


def test_run_ffmpeg_writes_product(source: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    result = run_ffmpeg(fake_ffmpeg_args(), source=source, output=output, extra_args=("-c", "copy"))
    assert result == output
    assert output.is_file() and output.stat().st_size > 0


def test_run_ffmpeg_reports_missing_executable(source: Path, tmp_path: Path):
    with pytest.raises(FFmpegError, match="未找到 ffmpeg"):
        run_ffmpeg(
            "liverreview-no-such-ffmpeg",
            source=source,
            output=tmp_path / "out.mp4",
        )


def test_run_ffmpeg_reports_nonzero_exit(source: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_MODE", "fail")
    with pytest.raises(FFmpegError, match="执行失败（退出码 1）"):
        run_ffmpeg(fake_ffmpeg_args(), source=source, output=tmp_path / "out.mp4")


def test_run_ffmpeg_fails_when_no_product_written(source: Path, tmp_path: Path, monkeypatch):
    """退出码为 0 却没写出文件：不能当作成功，否则下游会拿空片段继续上传。"""
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_MODE", "empty")
    with pytest.raises(FFmpegError, match="未产出可用的输出文件"):
        run_ffmpeg(fake_ffmpeg_args(), source=source, output=tmp_path / "out.mp4")


def test_run_ffmpeg_reports_missing_source(tmp_path: Path):
    with pytest.raises(FFmpegError, match="不存在或不是普通文件"):
        run_ffmpeg(fake_ffmpeg_args(), source=tmp_path / "absent.ts", output=tmp_path / "o.mp4")


def test_fake_ffmpeg_rejects_output_option_before_input(source: Path, tmp_path: Path):
    """替身也必须拦住错位的参数。

    回归用例：真实 ffmpeg 报「Option map cannot be applied to input url」，
    而最早那版替身对参数顺序照单全收，导致本地测试全绿、测试环境才炸。
    """
    with pytest.raises(FFmpegError, match="cannot be applied to input"):
        run_ffmpeg(
            fake_ffmpeg_args(),
            source=source,
            output=tmp_path / "out.mp4",
            input_args=("-map", "0:v", "-c", "copy"),
        )


def test_fake_ffmpeg_rejects_input_option_after_input(source: Path, tmp_path: Path):
    with pytest.raises(FFmpegError, match="move this option before the file"):
        run_ffmpeg(
            fake_ffmpeg_args(),
            source=source,
            output=tmp_path / "out.mp4",
            extra_args=("-fflags", "+genpts"),
        )


def test_fake_ffmpeg_mirrors_end_timestamp_semantics(source: Path, tmp_path: Path):
    """替身必须如实复刻 `-to` 的语义，否则「用 -to 限定切片长度」在本地查不出来。

    回归用例：`-to` 是输入时间轴上的绝对时刻，输入侧 `-ss 1351.672` 并不会把它平移，
    于是计划 1351.7s 的片段会切出 2703.3s（测试环境实测 2703.7s）。
    """
    output = tmp_path / "clip_001.mp4"
    run_ffmpeg(
        fake_ffmpeg_args(),
        source=source,
        output=output,
        input_args=("-ss", "1351.672"),
        extra_args=("-to", "2703.344"),
    )
    payload = json.loads(
        Path(f"{output}{SIDECAR_SUFFIX}").read_text(encoding="utf-8")
    )
    assert payload["duration_seconds"] == pytest.approx(1351.672, abs=0.01)


def test_run_ffmpeg_times_out(source: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_MODE", "timeout")
    with pytest.raises(FFmpegError, match="执行超时"):
        run_ffmpeg(fake_ffmpeg_args(), source=source, output=tmp_path / "o.mp4", timeout_seconds=1)


# —— 转封装 ——


def test_convert_to_mp4_produces_mp4_metadata(source: Path, tmp_path: Path):
    output = tmp_path / "converted.mp4"
    metadata = convert_to_mp4(
        source,
        output,
        original_metadata=_metadata(600.0),
        ffmpeg_command=fake_ffmpeg_args(),
        ffprobe_command=fake_ffprobe_args(),
    )
    assert output.is_file()
    assert is_target_container(metadata.format_name)
    assert metadata.duration_seconds == pytest.approx(600.0, abs=0.01)
    assert metadata.video is not None
    assert metadata.audio is not None


def test_convert_to_mp4_rejects_truncated_product(source: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_SHAPE", "truncated")
    with pytest.raises(FFmpegError, match="产物可能被截断"):
        convert_to_mp4(
            source,
            tmp_path / "converted.mp4",
            original_metadata=_metadata(600.0),
            ffmpeg_command=fake_ffmpeg_args(),
            ffprobe_command=fake_ffprobe_args(),
        )


def test_convert_to_mp4_accepts_a_source_whose_duration_includes_gaps(
    source: Path, tmp_path: Path, monkeypatch
):
    """端到端回归：源的标称时长含空洞、产物不含，转换必须照常通过。

    造一份源标称 605s、产物 600s 的场景（偏差 5.0s，超出 3.0s 容差）——线上那份
    1683.2s 素材就是同一形状。按可播放时长比，两侧都是清单的首尾跨度，不受标称时长影响。
    """
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_COUNT", "3")
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_GAP_AT", "1")
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PACKET_GAP_SECONDS", "4.0")

    metadata = convert_to_mp4(
        source,
        tmp_path / "converted.mp4",
        original_metadata=_metadata(605.0),
        ffmpeg_command=fake_ffmpeg_args(),
        ffprobe_command=fake_ffprobe_args(),
    )

    assert metadata.duration_seconds == pytest.approx(600.0, abs=0.01)


def test_convert_to_mp4_rejects_a_product_with_less_playable_content(
    source: Path, tmp_path: Path, monkeypatch
):
    """产物只切出一半内容时必须判失败：这是改比可播放时长后仍然要守住的红线。"""
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_PRODUCT_PACKET_RATIO", "0.5")

    with pytest.raises(FFmpegError, match="产物可能被截断"):
        convert_to_mp4(
            source,
            tmp_path / "converted.mp4",
            original_metadata=_metadata(600.0),
            ffmpeg_command=fake_ffmpeg_args(),
            ffprobe_command=fake_ffprobe_args(),
        )


def test_convert_to_mp4_rejects_a_truncated_product(source: Path, tmp_path: Path, monkeypatch):
    """产物标称时长明显偏短时照旧判失败（这里的替身连标称时长也减半）。"""
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_SHAPE", "truncated")

    with pytest.raises(FFmpegError, match="产物可能被截断"):
        convert_to_mp4(
            source,
            tmp_path / "converted.mp4",
            original_metadata=_metadata(600.0),
            ffmpeg_command=fake_ffmpeg_args(),
            ffprobe_command=fake_ffprobe_args(),
        )


def test_convert_to_mp4_still_works_when_timeline_unmeasurable(
    source: Path, tmp_path: Path, monkeypatch
):
    """时间轴统计失败时退回标称时长比对，正常素材不该因此失败。"""
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_MODE", "packets-fail")

    metadata = convert_to_mp4(
        source,
        tmp_path / "converted.mp4",
        original_metadata=_metadata(600.0),
        ffmpeg_command=fake_ffmpeg_args(),
        ffprobe_command=fake_ffprobe_args(),
    )

    assert metadata.duration_seconds == pytest.approx(600.0, abs=0.01)


def test_prepare_split_source_skips_conversion_for_mp4(source: Path, tmp_path: Path, monkeypatch):
    """已是 MP4 时直接复用原文件，不再调用 ffmpeg。"""
    log = tmp_path / "ffmpeg-log.txt"
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_LOG", str(log))

    path, metadata, converted = prepare_split_source(
        source,
        tmp_path / "converted.mp4",
        original_metadata=_metadata(600.0, format_name="mov,mp4,m4a,3gp,3g2,mj2"),
        ffmpeg_command=fake_ffmpeg_args(),
        ffprobe_command=fake_ffprobe_args(),
    )

    assert path == source
    assert converted is False
    assert metadata.duration_seconds == 600.0
    assert not log.exists(), "已是 MP4 却仍调用了 ffmpeg"


def test_prepare_split_source_converts_non_mp4(source: Path, tmp_path: Path):
    target = tmp_path / "converted.mp4"
    path, metadata, converted = prepare_split_source(
        source,
        target,
        original_metadata=_metadata(600.0),
        ffmpeg_command=fake_ffmpeg_args(),
        ffprobe_command=fake_ffprobe_args(),
    )

    assert path == target
    assert converted is True
    assert target.is_file()
    assert is_target_container(metadata.format_name)
    # 产物旁留有 sidecar，证明走的是伪造工具；真实环境由 ffprobe 直接读文件
    assert Path(f"{target}{SIDECAR_SUFFIX}").is_file()


# —— 单片切分与校验 ——


def test_cut_clip_writes_and_validates_product(tmp_path: Path):
    source = tmp_path / "converted.mp4"
    source.write_bytes(b"\0" * (64 * 1024))

    planned = ClipPlan(index=1, start_seconds=100.0, end_seconds=160.0)
    output = tmp_path / "clip_001.mp4"
    metadata = cut_clip(
        source,
        output,
        planned=planned,
        ffmpeg_command=fake_ffmpeg_args(),
        ffprobe_command=fake_ffprobe_args(),
    )

    assert output.is_file()
    assert metadata.duration_seconds == pytest.approx(60.0, abs=0.01)
    assert metadata.audio is not None


def test_validate_clip_rejects_missing_audio():
    planned = ClipPlan(index=0, start_seconds=0.0, end_seconds=60.0)
    with pytest.raises(SplitError, match="丢失了声音"):
        validate_clip(_metadata(60.0, audio=False, format_name="mp4"), planned=planned, size_bytes=1024)


def test_validate_clip_rejects_missing_video():
    planned = ClipPlan(index=0, start_seconds=0.0, end_seconds=60.0)
    with pytest.raises(SplitError, match="没有视频流"):
        validate_clip(
            _metadata(60.0, video=False, format_name="mp4"), planned=planned, size_bytes=1024
        )


def test_validate_clip_rejects_empty_product():
    planned = ClipPlan(index=0, start_seconds=0.0, end_seconds=60.0)
    with pytest.raises(SplitError, match="体积为 0"):
        validate_clip(_metadata(60.0, format_name="mp4"), planned=planned, size_bytes=0)


def test_validate_clip_does_not_judge_size():
    """体积由任务体的收敛过程负责：超限要对半重切，而不是在单片校验这里判失败。"""
    planned = ClipPlan(index=0, start_seconds=0.0, end_seconds=60.0)
    validate_clip(_metadata(60.0, format_name="mp4"), planned=planned, size_bytes=2 * 1024 * MiB)


def test_validate_clip_rejects_duration_mismatch():
    planned = ClipPlan(index=0, start_seconds=0.0, end_seconds=600.0)
    with pytest.raises(SplitError, match="与计划"):
        validate_clip(_metadata(60.0, format_name="mp4"), planned=planned, size_bytes=1024)


def test_validate_clip_allows_keyframe_induced_deviation():
    """-c copy 的切点落在关键帧上，几秒内的偏差属正常。"""
    planned = ClipPlan(index=0, start_seconds=0.0, end_seconds=600.0)
    validate_clip(_metadata(596.5, format_name="mp4"), planned=planned, size_bytes=1024)


def test_cut_clip_reports_ffmpeg_failure(tmp_path: Path, monkeypatch):
    source = tmp_path / "converted.mp4"
    source.write_bytes(b"\0" * (64 * 1024))
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_MODE", "fail")

    with pytest.raises(FFmpegError, match="执行失败"):
        cut_clip(
            source,
            tmp_path / "clip_000.mp4",
            planned=ClipPlan(index=0, start_seconds=0.0, end_seconds=60.0),
            ffmpeg_command=fake_ffmpeg_args(),
            ffprobe_command=fake_ffprobe_args(),
        )
