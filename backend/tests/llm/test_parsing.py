"""模型输出解析与时间对齐的测试（不触网）。"""

from __future__ import annotations

import pytest

from app.llm.parsing import (
    align_segments,
    extract_text,
    parse_response,
    parse_segments,
    strip_code_fence,
)
from tests.fake_llm import EXPLAINED_JSON, FENCED_JSON, SAMPLE_JSON, _FakeResponse


# —— 输出提取与围栏剥离 ——


def test_extract_text_reads_only_output_text_blocks():
    """只认 output_text 内容块；其它块（如推理）不是识别结果。"""
    response = _FakeResponse("正文")
    assert extract_text(response) == "正文"

    response.output.append(
        type(
            "Item",
            (),
            {"content": [type("Block", (), {"type": "reasoning", "text": "推理内容"})()]},
        )()
    )
    assert extract_text(response) == "正文"


@pytest.mark.parametrize("text", [SAMPLE_JSON, FENCED_JSON, EXPLAINED_JSON])
def test_parse_accepts_common_output_shapes(text):
    """纯数组、代码围栏包裹、前后夹带说明文字，三种形态都要能解析。"""
    segments, warnings = parse_segments(text)
    assert [segment.content for segment in segments] == ["欢迎来到直播间", "今天这款到手价 199 元"]
    assert warnings == []


def test_parse_accepts_object_wrapped_array_and_single_item():
    """数组被包在对象里、以及只识别到一句话时直接返回对象，都要能读出来。"""
    wrapped, _ = parse_segments('{"segments": [{"start_time": 1, "end_time": 2, "content": "甲"}]}')
    assert [segment.content for segment in wrapped] == ["甲"]

    single, _ = parse_segments('{"start_time": 1, "end_time": 2, "content": "乙"}')
    assert [segment.content for segment in single] == ["乙"]


def test_strip_code_fence_keeps_plain_text():
    assert strip_code_fence(SAMPLE_JSON) == SAMPLE_JSON


# —— 取值归一 ——


def test_parse_normalises_time_strings():
    """`00:12.5` 这类写法按分秒换算；单位（秒/毫秒）判断不在这里做。"""
    text = '[{"start_time": "00:10.5", "end_time": "00:12", "content": "分秒"}]'
    segments, _ = parse_segments(text)
    assert [(round(s.start_seconds, 1), round(s.end_seconds, 1)) for s in segments] == [(10.5, 12.0)]


def test_parse_sorts_by_start_time():
    """条目按开始时间排序：模型给出的顺序不保证与时间轴一致。"""
    text = (
        '[{"start_time": 9, "end_time": 10, "content": "后"},'
        ' {"start_time": 1, "end_time": 2, "content": "前"}]'
    )
    segments, _ = parse_segments(text)
    assert [segment.content for segment in segments] == ["前", "后"]


def test_parse_collapses_reversed_boundary_within_tolerance():
    """相邻句子的边界被写反且差值极小时按瞬时语句收敛，不丢弃整条记录。"""
    segments, warnings = parse_segments(
        '[{"start_time": 5.0, "end_time": 4.98, "content": "边界"}]'
    )
    assert warnings == []
    assert segments[0].start_seconds == segments[0].end_seconds == 5.0


# —— 不合规条目必须留痕 ——


def test_parse_reports_dropped_items_instead_of_silently_skipping():
    """丢弃的条目一律留下告警：识别缺口要显式可查。"""
    text = (
        '[{"start_time": 1, "end_time": 2, "content": "有效"},'
        ' {"start_time": 1, "content": "缺结束时间"},'
        ' {"start_time": 5, "end_time": 2, "content": "时间倒置"},'
        ' {"start_time": 6, "end_time": 7, "content": "   "},'
        ' "不是对象"]'
    )
    segments, warnings = parse_segments(text)
    assert [segment.content for segment in segments] == ["有效"]
    assert len(warnings) == 4
    assert "第 2 条" in warnings[0] and "第 5 条" in warnings[3]


def test_parse_rejects_non_json_output():
    """没有 JSON 数组就是失败，不能当成「片段里没人说话」。"""
    with pytest.raises(ValueError):
        parse_segments("这段视频里有很多人在讲话。")
    with pytest.raises(ValueError):
        parse_segments("   ")


def test_parse_returns_empty_list_for_empty_array():
    """空数组是有效输出：模型明确说了这里没有人声。"""
    segments, warnings = parse_segments("[]")
    assert segments == []
    assert warnings == []


def test_parse_response_carries_usage_and_raw_text():
    result = parse_response(_FakeResponse(SAMPLE_JSON, usage={"total_tokens": 123}))
    assert result.usage == {"total_tokens": 123}
    assert result.raw_text == SAMPLE_JSON
    assert len(result.segments) == 2


# —— 时间对齐 ——


def test_align_shifts_relative_time_onto_original_axis():
    """片段内相对时间加上片段起点，得到原视频时间轴上的绝对时间。"""
    result = parse_response(_FakeResponse(SAMPLE_JSON))
    aligned = align_segments(result, clip_start_seconds=600.0, clip_end_seconds=900.0)

    assert [round(item.start_seconds, 1) for item in aligned] == [600.5, 604.0]
    # 片段内相对时间同时保留，便于对照原始模型输出
    assert [item.clip_start_seconds for item in aligned] == [0.5, 4.0]
    assert all(not item.out_of_range for item in aligned)


def test_align_marks_out_of_range_without_clipping():
    """越界时段只标记不裁切：裁切会伪造模型没有给出的证据。"""
    # 片段长度 100s，单位判定阈值是 201s（超出即按毫秒折算），因此越界样例取 200s 以内
    text = (
        '[{"start_time": 0, "end_time": 150, "content": "超出片段长度"},'
        ' {"start_time": -3, "end_time": 2, "content": "起点为负"}]'
    )
    result = parse_response(_FakeResponse(text))
    aligned = align_segments(result, clip_start_seconds=100.0, clip_end_seconds=200.0)

    by_content = {item.content: item for item in aligned}
    assert all(item.out_of_range for item in aligned)
    # 时间值原样平移，没有被夹到片段范围内
    assert by_content["超出片段长度"].start_seconds == 100.0
    assert by_content["超出片段长度"].end_seconds == 250.0
    assert by_content["起点为负"].start_seconds == 97.0
    assert by_content["起点为负"].end_seconds == 102.0


def test_align_converts_milliseconds_using_clip_length():
    """毫秒判定以片段时长为基准：300 秒片段里的 1500 只能是 1.5 秒。"""
    text = '[{"start_time": 1500, "end_time": 3200, "content": "毫秒"}]'
    result = parse_response(_FakeResponse(text))
    aligned = align_segments(result, clip_start_seconds=0.0, clip_end_seconds=300.0)

    assert [(round(item.start_seconds, 2), round(item.end_seconds, 2)) for item in aligned] == [
        (1.5, 3.2)
    ]
    assert aligned[0].out_of_range is False


def test_align_keeps_large_values_inside_a_long_clip():
    """同一个 3200 落在 3600 秒的片段里就是合法秒数，不能当成毫秒。"""
    text = '[{"start_time": 3200, "end_time": 3210, "content": "长片段"}]'
    result = parse_response(_FakeResponse(text))
    aligned = align_segments(result, clip_start_seconds=0.0, clip_end_seconds=3600.0)

    assert aligned[0].start_seconds == 3200.0
    assert aligned[0].out_of_range is False
