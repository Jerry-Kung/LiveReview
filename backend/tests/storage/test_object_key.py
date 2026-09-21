"""对象键生成规则测试：分层、唯一性与扩展名保留。"""

import re

import pytest

from app.storage.base import (
    KNOWN_KINDS,
    OBJECT_KIND_CLIP,
    OBJECT_KIND_ORIGINAL,
    build_object_key,
)

# 期望形态：{前缀}{kind}/{文件名主体}_{毫秒时间戳}_{8位唯一串}{扩展名}
KEY_PATTERN = re.compile(
    r"^liverreview\/(?P<kind>[a-z]+)\/(?P<stem>[A-Za-z0-9._\-一-鿿]+)_(?P<ms>\d{10,})_(?P<uniq>[0-9a-f]{8})(?P<ext>\.[A-Za-z0-9]+)?$"
)


def test_key_follows_layered_layout():
    key = build_object_key("liverreview/", OBJECT_KIND_ORIGINAL, "直播录屏.ts", now_ms=1731657645123, unique="ab12cd34")
    assert key == "liverreview/original/直播录屏_1731657645123_ab12cd34.ts"
    assert KEY_PATTERN.match(key)


def test_kind_separates_objects_by_purpose():
    original = build_object_key("liverreview/", OBJECT_KIND_ORIGINAL, "a.ts", now_ms=1, unique="00000000")
    clip = build_object_key("liverreview/", OBJECT_KIND_CLIP, "a.ts", now_ms=1, unique="00000000")
    assert original != clip
    assert original.startswith("liverreview/original/")
    assert clip.startswith("liverreview/clip/")


def test_same_file_generated_twice_is_unique_even_with_frozen_clock():
    first = build_object_key("liverreview/", OBJECT_KIND_CLIP, "clip.ts", now_ms=1731657645123, unique="aaaaaaaa")
    second = build_object_key("liverreview/", OBJECT_KIND_CLIP, "clip.ts", now_ms=1731657645123, unique="bbbbbbbb")
    assert first != second


def test_real_calls_are_unique_within_same_millisecond():
    keys = {build_object_key("p/", OBJECT_KIND_CLIP, "clip.ts", now_ms=1731657645123) for _ in range(50)}
    assert len(keys) == 50


def test_extension_is_preserved_and_missing_extension_is_tolerated():
    assert build_object_key("p/", OBJECT_KIND_CLIP, "clip.mp4", now_ms=1, unique="00000000").endswith(".mp4")
    assert re.search(r"_00000000$", build_object_key("p/", OBJECT_KIND_CLIP, "noext", now_ms=1, unique="00000000"))


def test_dots_in_filename_do_not_lose_the_last_extension():
    key = build_object_key("p/", OBJECT_KIND_CLIP, "live.review.2024.ts", now_ms=1, unique="00000000")
    assert key.endswith("live.review.2024_1_00000000.ts")


def test_prefix_without_trailing_slash_is_normalised():
    assert build_object_key("liverreview", OBJECT_KIND_CLIP, "a.ts", now_ms=1, unique="00000000").startswith("liverreview/clip/")


def test_unsafe_stem_is_replaced():
    key = build_object_key("p/", OBJECT_KIND_CLIP, "直播 录屏@#$.ts", now_ms=1, unique="00000000")
    assert KEY_PATTERN.match(key) or re.match(r"^p/clip/[A-Za-z0-9._\-一-鿿]+_1_00000000\.ts$", key)


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        build_object_key("p/", "unknownkind", "a.ts", now_ms=1, unique="00000000")


def test_known_kinds_contains_the_two_v01_kinds():
    assert {OBJECT_KIND_ORIGINAL, OBJECT_KIND_CLIP} <= KNOWN_KINDS
