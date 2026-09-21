"""切分计划与覆盖校验的纯函数测试：不碰文件系统、不起子进程。

这两块是 V0.1.5 的正确性核心（顺序、双约束、覆盖关系），因此用固定数字充分覆盖：
分段均分、体积超限递归对半、顺序保持、边界容差与各类覆盖缺陷。
"""

from __future__ import annotations

import pytest

from app.media.coverage import (
    ISSUE_GAP,
    ISSUE_MISSING_END,
    ISSUE_MISSING_START,
    ISSUE_ORDER,
    ISSUE_OVERLAP,
    check_coverage,
    issues_from_json,
    issues_to_json,
)
from app.media.split import SplitError, plan_clips, plan_initial_clips, refine_by_size

MiB = 1024 * 1024


# —— 时长约束 ——


def test_single_clip_when_duration_within_limit():
    clips = plan_initial_clips(600.0, 3600)
    assert len(clips) == 1
    assert clips[0].index == 0
    assert clips[0].start_seconds == 0.0
    assert clips[0].end_seconds == 600.0


def test_duration_limit_splits_evenly_and_keeps_order():
    clips = plan_initial_clips(3600.0, 1200)
    assert [clip.index for clip in clips] == [0, 1, 2]
    assert [clip.start_seconds for clip in clips] == [0.0, 1200.0, 2400.0]
    assert [clip.end_seconds for clip in clips] == [1200.0, 2400.0, 3600.0]
    # 顺序即时间轴顺序，且首尾相接无缺口
    for previous, following in zip(clips, clips[1:]):
        assert previous.end_seconds == following.start_seconds
    assert clips[0].start_seconds == 0.0
    assert clips[-1].end_seconds == 3600.0


def test_remainder_goes_to_last_clip_without_empty_tail():
    """余数并入末尾：不产生极短尾巴片段，也不产生空片段。"""
    clips = plan_initial_clips(2500.0, 1200)
    assert len(clips) == 3
    assert all(clip.duration_seconds > 0 for clip in clips)
    assert clips[-1].end_seconds == 2500.0
    # 均分后每片长度一致，末片带余数因而略长
    assert clips[0].duration_seconds == clips[1].duration_seconds
    assert clips[-1].duration_seconds >= clips[0].duration_seconds


def test_every_clip_respects_duration_limit():
    for total in (1.0, 59.9, 60.0, 61.0, 3599.0, 3600.0, 3601.0, 20_000.0):
        clips = plan_initial_clips(total, 3600)
        assert clips[-1].end_seconds == pytest.approx(total, abs=0.001)
        for clip in clips:
            assert clip.duration_seconds <= 3600 + 0.001


def test_invalid_inputs_raise_split_error():
    with pytest.raises(SplitError):
        plan_initial_clips(0.0, 3600)
    with pytest.raises(SplitError):
        plan_initial_clips(-5.0, 3600)
    with pytest.raises(SplitError):
        plan_initial_clips(100.0, 0)


# —— 体积约束 ——


def test_no_refinement_when_all_clips_fit():
    clips = plan_initial_clips(3600.0, 1200)
    measured = [(0.0, 1200.0, 100 * MiB), (1200.0, 2400.0, 100 * MiB), (2400.0, 3600.0, 100 * MiB)]
    refined = refine_by_size(clips, max_clip_bytes=1024 * MiB, measured=measured)
    assert len(refined) == 3
    assert [(clip.start_seconds, clip.end_seconds) for clip in refined] == [
        (0.0, 1200.0),
        (1200.0, 2400.0),
        (2400.0, 3600.0),
    ]


def test_oversized_clip_is_bisected_in_place_keeping_order():
    """超体积的片段在原位置对半展开：顺序不变，缺口与重叠都不产生。"""
    clips = plan_initial_clips(3600.0, 3600)  # 单片 3600s
    measured = [(0.0, 3600.0, 4 * 1024 * MiB)]
    refined = refine_by_size(clips, max_clip_bytes=1024 * MiB, measured=measured)

    assert len(refined) == 4
    assert [clip.index for clip in refined] == [0, 1, 2, 3]
    assert refined[0].start_seconds == 0.0
    assert refined[-1].end_seconds == 3600.0
    for previous, following in zip(refined, refined[1:]):
        assert previous.end_seconds == following.start_seconds
    for clip in refined:
        assert clip.duration_seconds == pytest.approx(900.0)


def test_only_oversized_clip_is_refined():
    clips = plan_initial_clips(3600.0, 1200)
    measured = [
        (0.0, 1200.0, 100 * MiB),  # 达标
        (1200.0, 2400.0, 2 * 1024 * MiB),  # 超限 → 对半成两片各 1GiB，达标
        (2400.0, 3600.0, 100 * MiB),  # 达标
    ]
    refined = refine_by_size(clips, max_clip_bytes=1024 * MiB, measured=measured)

    assert [(clip.start_seconds, clip.end_seconds) for clip in refined] == [
        (0.0, 1200.0),
        (1200.0, 1800.0),
        (1800.0, 2400.0),
        (2400.0, 3600.0),
    ]
    assert [clip.index for clip in refined] == [0, 1, 2, 3]


def test_oversized_clip_is_bisected_repeatedly_until_it_fits():
    """体积远超上限时反复对半，直到每一半都达标；顺序与覆盖关系始终保持。"""
    clips = plan_initial_clips(3600.0, 3600)
    measured = [(0.0, 3600.0, 3 * 1024 * MiB)]
    refined = refine_by_size(clips, max_clip_bytes=1024 * MiB, measured=measured)

    # 3GiB → 两半各 1.5GiB（仍超限）→ 四片各 900s / 0.75GiB，达标
    assert len(refined) == 4
    assert [(clip.start_seconds, clip.end_seconds) for clip in refined] == [
        (0.0, 900.0),
        (900.0, 1800.0),
        (1800.0, 2700.0),
        (2700.0, 3600.0),
    ]
    assert [clip.index for clip in refined] == [0, 1, 2, 3]
    # 顺序即时间轴顺序，首尾相接无缺口
    assert refined[0].start_seconds == 0.0
    assert refined[-1].end_seconds == 3600.0
    for previous, following in zip(refined, refined[1:]):
        assert previous.end_seconds == following.start_seconds


def test_unmeasured_clip_is_kept_as_is():
    """没有实测体积的片段按「无法判断」保留：不臆造体积。"""
    clips = plan_initial_clips(3600.0, 1200)
    refined = refine_by_size(clips, max_clip_bytes=1024 * MiB, measured=[])
    assert len(refined) == 3


def test_refinement_fails_when_size_constraint_is_unreachable():
    """到达细分下限仍超限时判失败，而不是无限细分。"""
    clips = plan_initial_clips(10.0, 3600)
    measured = [(0.0, 10.0, 100 * MiB)]
    with pytest.raises(SplitError, match="无法在满足体积约束"):
        refine_by_size(clips, max_clip_bytes=1024, measured=measured, min_clip_seconds=1.0)


def test_invalid_max_clip_bytes_raises():
    clips = plan_initial_clips(100.0, 3600)
    with pytest.raises(SplitError):
        refine_by_size(clips, max_clip_bytes=0, measured=[(0.0, 100.0, 10)])


def test_plan_clips_combines_both_constraints():
    """时长先均分、体积再对半：两个约束同时生效，顺序仍然正确。"""
    clips = plan_clips(
        7200.0,
        max_duration_seconds=3600,
        max_clip_bytes=1024 * MiB,
        measured=[(0.0, 3600.0, 2 * 1024 * MiB)],
    )
    # 首片时限达标但体积超限 → 对半成两片各 1GiB；第二片无实测则原样保留
    assert [(clip.start_seconds, clip.end_seconds) for clip in clips] == [
        (0.0, 1800.0),
        (1800.0, 3600.0),
        (3600.0, 7200.0),
    ]
    for previous, following in zip(clips, clips[1:]):
        assert previous.end_seconds == following.start_seconds


# —— 覆盖校验 ——


def test_coverage_passes_for_contiguous_clips():
    issues = check_coverage(3600.0, [(0.0, 1200.0), (1200.0, 2400.0), (2400.0, 3600.0)])
    assert issues == []


def test_coverage_reports_missing_start():
    issues = check_coverage(3600.0, [(10.0, 1200.0), (1200.0, 3600.0)])
    assert [issue.code for issue in issues] == [ISSUE_MISSING_START]


def test_coverage_reports_missing_end():
    issues = check_coverage(3600.0, [(0.0, 1200.0), (1200.0, 3000.0)])
    assert [issue.code for issue in issues] == [ISSUE_MISSING_END]


def test_coverage_reports_gap_between_clips():
    issues = check_coverage(3600.0, [(0.0, 1200.0), (1260.0, 3600.0)])
    assert [issue.code for issue in issues] == [ISSUE_GAP]
    assert "缺失 60.0s" in issues[0].message


def test_coverage_reports_overlap_between_clips():
    issues = check_coverage(3600.0, [(0.0, 1300.0), (1200.0, 3600.0)])
    assert [issue.code for issue in issues] == [ISSUE_OVERLAP]


def test_coverage_reports_out_of_order_clips():
    issues = check_coverage(3600.0, [(1200.0, 2400.0), (0.0, 1200.0)])
    codes = [issue.code for issue in issues]
    assert ISSUE_ORDER in codes
    assert ISSUE_MISSING_START in codes


def test_coverage_within_tolerance_passes():
    """-c copy 的切点落在关键帧上，1 秒内的偏差不算缺口。"""
    issues = check_coverage(3600.0, [(0.0, 1199.5), (1199.5, 2400.4), (2400.4, 3599.6)])
    assert issues == []


def test_coverage_reports_empty_clip_list():
    issues = check_coverage(3600.0, [])
    assert [issue.code for issue in issues] == [ISSUE_MISSING_END]


def test_coverage_with_unknown_total_duration_still_checks_boundaries():
    issues = check_coverage(None, [(0.0, 1200.0), (1300.0, 3600.0)])
    assert [issue.code for issue in issues] == [ISSUE_GAP]


def test_coverage_issues_round_trip_through_json():
    issues = check_coverage(3600.0, [(10.0, 1200.0)])
    raw = issues_to_json(issues)
    assert raw is not None
    assert issues_from_json(raw) == [
        {"code": issue.code, "message": issue.message} for issue in issues
    ]


def test_no_issues_serialises_to_none():
    """无问题入库为 None，使「校验通过」与「尚未校验」在库中可区分。"""
    assert issues_to_json([]) is None
    assert issues_from_json(None) == []
    assert issues_from_json("not json") == []
    assert issues_from_json('{"unexpected": 1}') == []
