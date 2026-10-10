"""复盘归一的新增字段测试（V0.8.0）。

覆盖三类新增字段的收敛行为，重点是**编号**：

- 编号列表（`principle_codes` / `script_codes`）：模型给的编号必须对着数据源校验。不存在的
  编号若被原样留下，报告里就会出现一个查不到出处的指代；反之，能识别出来的写法（数字、带前缀
  的文本）都要救回来，不能因为模型没严格按「04」写就整条丢掉。
- 布尔（`has_increment`）与整数（`count`）：不能走文本归一，否则布尔被抹成空串、次数变成
  字符串，界面上「重复 3 次」就没法比较。
- 金句、违规词、重复度三个新列表：缺字段时补空数组，条目缺关键内容时丢弃并记账。
"""

from __future__ import annotations

from app.llm.parsing import normalise_review


def _base(**extra):
    """一份最小可归一的对象。

    `analysis_level` 必须带上：归一时取值无法识别的等级会留一条告警，缺了它每条用例的
    `warnings == []` 都会被这条无关告警打破，测试就测不出真正要测的东西。
    """
    payload = {"one_line": "本场结论", "analysis_level": "部分"}
    payload.update(extra)
    return payload


def test_principle_and_script_codes_are_kept_when_valid():
    """合法编号原样保留，且去重、保序。"""
    payload, warnings = normalise_review(
        _base(
            findings=[
                {
                    "dimension": "产品讲解",
                    "judgement": "缺少场景翻译",
                    "principle_codes": ["心法11", "心法1", "心法11"],
                }
            ],
            next_actions=[
                {
                    "goal": "报价前补场景",
                    "script_codes": ["02", "09"],
                }
            ],
        )
    )
    assert warnings == []
    assert payload["findings"][0]["principle_codes"] == ["心法11", "心法1"]
    assert payload["next_actions"][0]["script_codes"] == ["02", "09"]


def test_unknown_codes_are_recorded_not_silently_kept():
    """不存在的编号被丢弃并留告警：报告里不能出现查不到出处的指代。"""
    payload, warnings = normalise_review(
        _base(
            next_actions=[
                {"goal": "补场景", "script_codes": ["02", "99", "我编的"]},
            ]
        )
    )
    assert payload["next_actions"][0]["script_codes"] == ["02"]
    assert any("99" in item for item in warnings)
    assert any("我编的" in item for item in warnings)


def test_numeric_and_prefixed_codes_are_recovered():
    """数字与带前缀的写法要能救回来：不允许因为写法不严格就丢掉整条引用。"""
    payload, warnings = normalise_review(
        _base(
            findings=[{"judgement": "缺钩子", "principle_codes": ["心法3", 3]}],
            top_issues=[
                {
                    "problem": "没有钩子",
                    "principle_codes": ["心法3"],
                    "script_codes": [4, "话术样板05"],
                }
            ],
        )
    )
    # 数字 3 补成「心法3」后与已有编号重复，去重只留一个
    assert payload["findings"][0]["principle_codes"] == ["心法3"]
    assert payload["top_issues"][0]["script_codes"] == ["04", "05"]
    assert warnings == []


def test_code_fields_accept_single_string():
    """只给一个编号（不是列表）也要收下：模型常见写法。"""
    payload, warnings = normalise_review(
        _base(top_issues=[{"problem": "问题", "script_codes": "04"}])
    )
    assert payload["top_issues"][0]["script_codes"] == ["04"]
    assert warnings == []


def test_counts_and_bools_keep_their_type():
    """次数是整数、是否有增量是布尔：不能退化成字符串，否则界面无法比较。"""
    payload, _ = normalise_review(
        _base(
            repetition=[
                {
                    "item": "900mm 涉水",
                    "count": 4,
                    "has_increment": False,
                    "verdict": "同质复读，计入问题项",
                },
                {
                    "item": "三把差速锁",
                    "count": "2",
                    "has_increment": "是",
                    "verdict": "第二次讲了场景，有增量",
                },
            ]
        )
    )
    first, second = payload["repetition"]
    assert first["count"] == 4 and first["has_increment"] is False
    assert second["count"] == 2 and second["has_increment"] is True


def test_quotes_and_violations_are_normalised():
    """金句与违规词条目齐全，各自带时间或替换说法。"""
    payload, warnings = normalise_review(
        _base(
            quotes=[
                {
                    "start_seconds": 120.0,
                    "text": "国有大事，必有猛士",
                    "category": "品牌叙事类",
                    "scene": "讲品牌故事处",
                    "effect": "契合品牌调性",
                }
            ],
            violations=[
                {"term": "全网最低", "quote": "我们全网最低价", "replacement": "我们店里这价", "start_seconds": 300.0}
            ],
        )
    )
    assert warnings == []
    quote = payload["quotes"][0]
    assert quote["text"] == "国有大事，必有猛士"
    assert quote["start_seconds"] == 120.0
    assert quote["category"] == "品牌叙事类"
    violation = payload["violations"][0]
    assert violation["term"] == "全网最低"
    assert violation["replacement"] == "我们店里这价"
    assert violation["start_seconds"] == 300.0


def test_new_lists_default_to_empty_arrays():
    """三个新字段缺失时补空数组：老任务落库的结论里没有它们，界面不该到处判空。"""
    payload, warnings = normalise_review(_base())
    assert payload["quotes"] == []
    assert payload["violations"] == []
    assert payload["repetition"] == []
    # 既有字段不受影响
    assert payload["findings"] == []
    assert payload["top_issues"] == []
    assert warnings == []


def test_items_without_content_are_dropped_with_warning():
    """新列表里缺关键内容的条目被丢弃并记账，不让空壳条目进结论。"""
    payload, warnings = normalise_review(
        _base(
            quotes=[{"category": "开场钩子类"}],
            repetition=[{"count": 3}],
            violations=[{"replacement": "我们店里这价"}],
        )
    )
    assert payload["quotes"] == []
    assert payload["repetition"] == []
    assert payload["violations"] == []
    assert len(warnings) == 3


def test_topic_distribution_is_kept():
    """主题分布是结构轮的文本产出：与一句话结论一样按文本归一，不能被丢掉。

    它是「这场讲了什么」的分布口径，报告里单独成节。漏掉时不会产生任何告警——结论里
    只是安静地少了一节，因此这条用例钉的是「它在归一化后仍然存在」。
    """
    payload, warnings = normalise_review(_base(topic_distribution="以 A 产品讲解为主，F 权益政策集中在中段"))
    assert payload["topic_distribution"] == "以 A 产品讲解为主，F 权益政策集中在中段"
    assert warnings == []


def test_topic_distribution_defaults_to_empty_string():
    """非结构轮没有这个字段：缺失时补空串，与既有文本字段的处理一致。"""
    payload, warnings = normalise_review(_base())
    assert payload["topic_distribution"] == ""
    assert warnings == []


def test_existing_fields_still_normalise_after_loop_change():
    """既有字段的归一不受新分支影响：这是改公共循环时最需要守住的一条。"""
    payload, warnings = normalise_review(
        _base(
            findings=[
                {
                    "dimension": "转化",
                    "judgement": "留资引导偏早",
                    "evidence_level": "事实",
                    "evidence": "现在下订直接减两万",
                    "start_seconds": 90.0,
                    "suggestion": "先讲价值",
                }
            ],
            next_actions=[
                {
                    "goal": "报价前完成价值翻译",
                    "how": "按场景讲三项配置",
                    "observe": "是否在报价前翻译",
                    "current_approach": "直接报参数",
                    "change_reason": "上一场建议改话术但未执行，本次改为调整内容顺序",
                    "success_criteria": "报价前出现≥3 次场景翻译",
                }
            ],
        )
    )
    assert warnings == []
    finding = payload["findings"][0]
    assert finding["dimension"] == "转化"
    assert finding["start_seconds"] == 90.0
    assert finding["principle_codes"] == []
    action = payload["next_actions"][0]
    assert action["current_approach"] == "直接报参数"
    assert action["success_criteria"] == "报价前出现≥3 次场景翻译"
    assert action["script_codes"] == []
