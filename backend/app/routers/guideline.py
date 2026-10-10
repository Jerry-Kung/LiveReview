"""复盘准则的编号对照表接口（V0.8.0）。

复盘结论里会写「使用话术样板 04」「缺失心法3 的钩子」这类带编号的指代。编号是系统与一线
业务之间的共同语言，前提是两端看到的是同一份定义——因此这个接口不分场景、不做裁剪，直接从
`app/llm/mindset.py` 读出提示词用的那份数据，原样发给界面。

权限与设置接口的读口同类：**对所有登录账号开放**。它是静态参考内容，不含任何任务数据、
不泄露运行配置，而主播与运营都需要查它；反过来，它不该像账号管理那样只给管理员——那样
拿到报告的普通账号就查不到编号含义了。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.llm.mindset import MIND_SETS, SCRIPT_SAMPLES, VIOLATION_TERMS
from app.schemas import (
    GuidelineMindsetResponse,
    GuidelineResponse,
    GuidelineSampleResponse,
    GuidelineTermResponse,
)

router = APIRouter(prefix="/api/guideline", tags=["guideline"])


@router.get("", response_model=GuidelineResponse)
def read_guideline() -> GuidelineResponse:
    """返回心法、话术样板与违规词库的完整对照表。

    不提供按编号查询的子接口：这三份数据的规模（12 + 30 + 5 条）一次全量取回就是最省的
    做法，拆分只会多出「界面忘了取哪一份」这类问题。
    """
    return GuidelineResponse(
        mindsets=[
            GuidelineMindsetResponse(
                code=item.code,
                name=item.name,
                check=item.check,
                sample_codes=list(item.sample_codes),
            )
            for item in MIND_SETS
        ],
        samples=[
            GuidelineSampleResponse(
                code=item.code,
                category=item.category,
                usage=item.usage,
                text=item.text,
            )
            for item in SCRIPT_SAMPLES
        ],
        violations=[
            GuidelineTermResponse(term=item.term, replacement=item.replacement)
            for item in VIOLATION_TERMS
        ],
    )
