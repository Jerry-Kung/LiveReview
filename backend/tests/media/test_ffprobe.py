"""ffprobe 输出解析的测试：全部基于固定 JSON，不依赖本机安装 ffprobe。"""

from __future__ import annotations

import json

import pytest

from app.media.ffprobe import ProbeParseError, has_playable_streams, parse_probe_output


def _output(streams: list[dict], fmt: dict | None = None) -> str:
    payload = {"streams": streams}
    if fmt is not None:
        payload["format"] = fmt
    return json.dumps(payload)


TS_FORMAT = {
    "format_name": "mpegts",
    "format_long_name": "MPEG-TS (MPEG-2 Transport Stream)",
    "duration": "N/A",  # TS 录屏常见：容器头没有可信时长
    "size": "1610612736",
    "bit_rate": "3500000",
    "probe_score": 50,
    "nb_streams": 2,
}

VIDEO_STREAM = {
    "index": 0,
    "codec_type": "video",
    "codec_name": "h264",
    "codec_long_name": "H.264 / AVC",
    "profile": "High",
    "width": 1920,
    "height": 1080,
    "avg_frame_rate": "25/1",
    "pix_fmt": "yuv420p",
    "nb_read_packets": "15000",
}

AUDIO_STREAM = {
    "index": 1,
    "codec_type": "audio",
    "codec_name": "aac",
    "sample_rate": "48000",
    "channels": 2,
    "channel_layout": "stereo",
}


def test_parses_container_and_both_streams():
    metadata = parse_probe_output(_output([VIDEO_STREAM, AUDIO_STREAM], TS_FORMAT))

    assert metadata.format_name == "mpegts"
    assert metadata.size_bytes == 1610612736
    assert metadata.bit_rate == 3500000
    assert metadata.probe_score == 50
    assert metadata.stream_count == 2
    assert metadata.video is not None
    assert metadata.video.codec_name == "h264"
    assert (metadata.video.width, metadata.video.height) == (1920, 1080)
    assert metadata.video.frame_rate == "25/1"
    assert metadata.audio is not None
    assert metadata.audio.sample_rate == 48000
    assert metadata.audio.channels == 2


def test_duration_falls_back_to_packet_count_when_header_says_na():
    """TS 录屏的 duration 常为 N/A：必须用「包计数 ÷ 帧率」补出时间基准。"""
    metadata = parse_probe_output(_output([VIDEO_STREAM, AUDIO_STREAM], TS_FORMAT))

    # 15000 帧 ÷ 25fps = 600 秒
    assert metadata.duration_seconds == pytest.approx(600.0)


def test_header_duration_wins_over_fallback():
    fmt = dict(TS_FORMAT, duration="12.5")
    metadata = parse_probe_output(_output([VIDEO_STREAM, AUDIO_STREAM], fmt))

    assert metadata.duration_seconds == pytest.approx(12.5)


def test_missing_stream_fields_become_none_not_zero():
    """字段缺失一律为 None：未知不能被当成 0，否则下游会把「没有音轨」当成「静音音轨」。"""
    metadata = parse_probe_output(
        _output([{"index": 0, "codec_type": "video", "codec_name": "h264"}], TS_FORMAT)
    )

    assert metadata.video is not None
    assert metadata.video.width is None
    assert metadata.video.height is None
    assert metadata.video.bit_rate is None
    assert metadata.audio is None


def test_video_only_source_has_no_audio_stream():
    metadata = parse_probe_output(_output([VIDEO_STREAM], TS_FORMAT))

    assert metadata.video is not None
    assert metadata.audio is None
    assert has_playable_streams(metadata) is True


def test_audio_only_source_has_no_video_stream():
    """纯音频（如只推流的电台）仍可探测：有任一可播放流即视为成功。"""
    metadata = parse_probe_output(_output([AUDIO_STREAM], TS_FORMAT))

    assert metadata.video is None
    assert metadata.audio is not None
    assert has_playable_streams(metadata) is True


def test_unknown_codec_type_is_kept_but_not_playable():
    metadata = parse_probe_output(
        _output([{"index": 0, "codec_type": "data", "codec_name": "bin_data"}], TS_FORMAT)
    )

    assert metadata.video is None
    assert metadata.audio is None
    assert has_playable_streams(metadata) is False


def test_duration_unavailable_when_no_packet_count_and_no_header_duration():
    fmt = dict(TS_FORMAT, duration="N/A")
    stream = dict(VIDEO_STREAM)
    stream.pop("nb_read_packets")
    metadata = parse_probe_output(_output([stream], fmt))

    assert metadata.duration_seconds is None


def test_negative_header_duration_is_discarded_and_falls_back_to_packets():
    """容器头的负时长是无效值，不能直接入库；回退到包计数给出的时间基准。"""
    fmt = dict(TS_FORMAT, duration="-1")
    metadata = parse_probe_output(_output([VIDEO_STREAM, AUDIO_STREAM], fmt))

    assert metadata.duration_seconds == pytest.approx(600.0)


def test_negative_header_duration_without_packet_count_stays_unknown():
    fmt = dict(TS_FORMAT, duration="-1")
    stream = dict(VIDEO_STREAM)
    stream.pop("nb_read_packets")
    metadata = parse_probe_output(_output([stream], fmt))

    assert metadata.duration_seconds is None


def test_missing_format_section_still_parses_streams():
    metadata = parse_probe_output(_output([VIDEO_STREAM, AUDIO_STREAM]))

    assert metadata.format_name is None
    assert metadata.stream_count == 2
    assert metadata.video is not None


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "空输出"),
        ("   ", "只有空白"),
        ("not json at all", "非 JSON"),
        ("[]", "顶层是数组"),
        ('{"streams": []}', "没有媒体流"),
        ('{"format": {"format_name": "mpegts"}}', "缺少 streams"),
    ],
)
def test_unusable_output_raises_parse_error(stdout, reason):
    with pytest.raises(ProbeParseError) as excinfo:
        parse_probe_output(stdout)

    assert str(excinfo.value)


def test_raw_json_is_preserved_for_troubleshooting():
    stdout = _output([VIDEO_STREAM, AUDIO_STREAM], TS_FORMAT)
    metadata = parse_probe_output(stdout)

    assert json.loads(metadata.raw_json)["format"]["format_name"] == "mpegts"


def test_summary_mentions_duration_resolution_and_codecs():
    metadata = parse_probe_output(_output([VIDEO_STREAM, AUDIO_STREAM], TS_FORMAT))
    summary = metadata.summary()

    assert "600.0s" in summary
    assert "1920x1080" in summary
    assert "h264" in summary
    assert "aac" in summary


def test_summary_of_streamless_metadata_does_not_crash():
    """摘要会被写日志，不能因为字段缺失而抛异常。"""
    from app.media.ffprobe import MediaMetadata

    assert MediaMetadata().summary()
