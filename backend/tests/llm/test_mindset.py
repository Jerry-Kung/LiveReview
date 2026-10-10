"""准则数据源的完整性测试（V0.8.0）。

这份数据是提示词与界面共用的**唯一**来源，编号一旦漂移，报告里的「话术样板 04」就会指向
另一个人说的话。因此这里断言的是数据的**结构不变量**，不是具体措辞——措辞会随准则版本
变，编号规则与覆盖范围不该跟着变。
"""

from __future__ import annotations

from app.llm import mindset


def test_mindset_codes_are_unique_and_contiguous():
    """12 条心法：编号唯一，且是从 心法1 到 心法12 的连续序列。

    连续性值得断言：准则按 心法1~12 引用，缺号意味着提取时漏了一条，而漏掉的那条在提示词
    里就永远不会被检核。
    """
    codes = [item.code for item in mindset.MIND_SETS]
    assert len(codes) == len(set(codes))
    assert codes == [f"心法{index}" for index in range(1, 13)]


def test_mindset_entries_are_complete():
    """每条心法都要有名称与检核项：缺检核项的心法等于一条无法执行的指令。"""
    for item in mindset.MIND_SETS:
        assert item.name.strip(), item.code
        assert item.check.strip(), item.code


def test_sample_codes_are_unique_and_within_range():
    """30 句话术样板：编号唯一、不重复，且都在 01~30 之间。"""
    codes = [item.code for item in mindset.SCRIPT_SAMPLES]
    assert len(codes) == 30
    assert len(codes) == len(set(codes))
    assert all(code.isdigit() and 1 <= int(code) <= 30 for code in codes)
    # 编号带前导零是准则的写法，也是报告里引用的形式；去掉前导零会让「04」对不上
    assert all(len(code) == 2 for code in codes)


def test_sample_text_is_non_empty():
    """每句样板都要有原文：只有编号没有原文的条目，界面查出来是一行空白。"""
    for item in mindset.SCRIPT_SAMPLES:
        assert item.text.strip(), item.code
        assert item.category.strip(), item.code


def test_mindset_sample_codes_all_exist():
    """心法引用的样板编号必须真实存在——指向空编号的引用比没有引用更糟。"""
    for item in mindset.MIND_SETS:
        for code in item.sample_codes:
            assert code in mindset.SAMPLE_CODES, f"{item.code} 引用了不存在的话术样板 {code}"


def test_sample_lookup_returns_none_for_unknown_code():
    """按编号取样板：存在的编号取得到，不存在的返回 None 而不是抛异常。"""
    first = mindset.SCRIPT_SAMPLES[0]
    assert mindset.sample_by_code(first.code) is first
    assert mindset.sample_by_code("99") is None
    assert mindset.sample_by_code("4") is None


def test_violation_terms_present():
    """违规词库不为空，且每条都给出替换说法：只列禁词不给替换，主播仍然不知道该说什么。"""
    assert mindset.VIOLATION_TERMS
    for item in mindset.VIOLATION_TERMS:
        assert item.term.strip(), item.term
        assert item.replacement.strip(), item.term


def test_renderers_cover_every_entry():
    """三个渲染函数必须覆盖全部条目：漏渲染的条目不会进提示词，模型也就检核不到。"""
    index_text = mindset.render_mindset_index()
    assert len(index_text.splitlines()) == len(mindset.MIND_SETS)
    for item in mindset.MIND_SETS:
        assert item.code in index_text
        assert item.check in index_text

    sample_text = mindset.render_script_samples()
    for item in mindset.SCRIPT_SAMPLES:
        assert item.text in sample_text
    # 分类标题单独占行，因此渲染行数 = 条目数 + 分类数
    categories = {item.category for item in mindset.SCRIPT_SAMPLES}
    assert len(sample_text.splitlines()) == len(mindset.SCRIPT_SAMPLES) + len(categories)

    violation_text = mindset.render_violation_terms()
    assert len(violation_text.splitlines()) == len(mindset.VIOLATION_TERMS)
    for item in mindset.VIOLATION_TERMS:
        assert item.replacement in violation_text


def test_renderers_do_not_leak_improve_guidance():
    """心法索引不带准则里的「改进」原文。

    那条原文是通用改法（如「建议开播前跟练丹田发声 30 分钟」），把它发给模型，模型就会
    照抄进结论——而准则 §二十五 第 11 条明确禁止这种与本场无关的空话。改进动作必须由模型
    结合本场实际产出。
    """
    index_text = mindset.render_mindset_index()
    for item in mindset.MIND_SETS:
        if item.improve:
            assert item.improve not in index_text, item.code
