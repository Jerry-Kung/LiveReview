"""婉婉心法与话术样板的单一数据源（准则 V3.2 §二十二~二十四）。

准则原文里的人名与编号是本系统与一线业务之间的共同语言：复盘结论里写「使用话术样板 04」，
主播与运营要能查到 04 说的是什么，因此这份数据同时供给两处——复盘提示词（把心法检核项与
样板原文发给模型）与只读接口（界面按编号显示原文）。两处共用一份数据，是为了避免「提示词
换了、界面没换」这类漂移：编号一旦对不上，报告就变成一句谁也看不懂的暗语。

数据是**裁剪**进来的，不是全文照搬：准则 §二十二 的「检核 / 改进 / 话术样板」三段全部保留，
因为它们是可直接执行的指令；§十四 里按维度排布的检核清单表不在这里，它属于提示词的编排，
见 `review_prompts.py`。

与准则的对应关系与裁剪口径记在 `docs/specs/v0.8.0-design.md`，改动本文件时两者要一起看。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mindset:
    """一条心法：编号、名称、检核项、达标的改进方向、相关话术样板编号。"""

    code: str
    name: str
    check: str
    improve: str
    sample_codes: tuple[str, ...]


@dataclass(frozen=True)
class ScriptSample:
    """一句可直接照念的话术样板。

    `usage` 是准则给出的用法标签（如「需求逼单」「破价拒单」），多数条目有，少数只有原文。
    """

    code: str
    category: str
    usage: str
    text: str


@dataclass(frozen=True)
class ViolationTerm:
    """一条违规表达与它的合规替换。"""

    term: str
    replacement: str


# 12 条心法：检核工具，也是改进动作的参照物。
MIND_SETS: tuple[Mindset, ...] = (
    Mindset(
        code="心法1",
        name="八爪鱼塑品框架",
        check="主播是否把卖点拆成≥4爪展开（核心/延伸/场景/对比/价格/竞争/服务/情感）",
        improve="未达标时，要求主播按8爪框架重写产品讲解脚本",
        sample_codes=("03", "19"),
    ),
    Mindset(
        code="心法2",
        name="播感四步开窍",
        check="丹田发声→穿透力→节奏感→感染力，主播当前在哪一步",
        improve="白嗓→开播前跟练丹田发声30分钟；缺节奏→刻意练抑扬顿挫",
        sample_codes=("16", "17"),
    ),
    Mindset(
        code="心法3",
        name="每分钟放钩子",
        check="统计全场钩子密度（个/分钟），钩子类型是否≥2种",
        improve="密度<1个/分钟→每10分钟插入一次强钩子；类型单一→轮换6种钩子",
        sample_codes=("01", "11", "12"),
    ),
    Mindset(
        code="心法4",
        name="逼单六板斧",
        check="是否组合使用需求+痛点+紧迫感三板斧（非价格逼单）",
        improve="缺逼单→按顺序连用需求→稀缺→痛点→紧迫感",
        sample_codes=("04", "05", "06", "07", "29"),
    ),
    Mindset(
        code="心法5",
        name="心态决定状态",
        check="低在线/无互动/被黑时，主播心态是否崩",
        improve="建立能量管理三板斧（能量分配/身体底线/心态修复）",
        sample_codes=("16", "17"),
    ),
    Mindset(
        code="心法6",
        name="急速流4到位",
        check="急速流来临时，留存率/互动/成交密度/关注是否到位",
        improve="10分钟内必须出留资动作，否则后续全掉",
        sample_codes=("15", "30", "26"),
    ),
    Mindset(
        code="心法7",
        name="\"每当你\"种草 + 五官塑品",
        check="是否用\"每当你…\"句式触发场景联想；是否调动五官感受",
        improve="缺场景→补\"每当你…\"框架；缺感官→补手握方向盘/涉水声音/扭矩感等",
        sample_codes=("01", "09"),
    ),
    Mindset(
        code="心法8",
        name="违规红线 + 不违规替换",
        check="是否出现\"最/第一/唯一/全网最低/绝对\"等违规词",
        improve="发现违规词立即标注并给出合规替换（见替换表）",
        sample_codes=("08", "07"),
    ),
    Mindset(
        code="心法9",
        name="起号四部曲",
        check="新主播是否按4天节奏训练（练嘴→练塑品→练节奏→练场控）",
        improve="新主播按此框架制定首月训练计划",
        sample_codes=(),
    ),
    Mindset(
        code="心法10",
        name="草根逆袭叙事",
        check="主播人设是否有\"抗住过程→拿到结果\"的叙事弧线",
        improve="帮主播挖掘3-5个真实故事，按叙事模板包装",
        sample_codes=("10", "21", "22", "23"),
    ),
    Mindset(
        code="心法11",
        name="\"穿上/用上之后的我\"",
        check="产品讲解是否≥50%是场景代入，≤50%是参数直给",
        improve="比例失衡→强制每个参数跟一个\"你开这台车…\"句式",
        sample_codes=("02", "09", "11"),
    ),
    Mindset(
        code="心法12",
        name="互动8大变体",
        check="全场是否使用≥3种互动类型",
        improve="类型<3种→每10分钟轮换一种（选款/地域/身份/场景/计时/问答/欢迎/刷屏）",
        sample_codes=("13", "14", "15", "25"),
    ),
)

# 30 句 M817 话术样板，按准则给的场景顺序排列（编号不连续，编号本身是对外的键）。
SCRIPT_SAMPLES: tuple[ScriptSample, ...] = (
    ScriptSample(code="01", category="开场钩子类", usage="", text="\"每当你周末想带家人出去，又怕城市SUV装不下/跑不到野外的时候，你就要想起——东风猛士M817\""),
    ScriptSample(code="11", category="开场钩子类", usage="急速流场景剧情钩", text="\"老板说今晚必须把这批物资送进山里…你的城市SUV已经在半路趴窝了\""),
    ScriptSample(code="16", category="开场钩子类", usage="状态钩子（破零场）", text="\"今天一开直播，零人在线…第一台车的口播我已经练了30遍\""),
    ScriptSample(code="26", category="开场钩子类", usage="强势突发拉停留", text="\"所有人听好——3声口令之后我会下架。3——2——1——抢！\""),
    ScriptSample(code="02", category="塑品类（产品讲解）", usage="场景代替参数", text="\"这台M817不是让你在城里开的，它是让你去工地、去山里、去灾区的\""),
    ScriptSample(code="03", category="塑品类（产品讲解）", usage="八爪鱼8爪展开", text="\"第一爪东风军工底盘…第二爪900mm涉水…第三爪三把差速锁…\""),
    ScriptSample(code="09", category="塑品类（产品讲解）", usage="种草框架\"穿上之后的我\"", text="\"你开这台车不是开了车，是开了一种生活\""),
    ScriptSample(code="19", category="塑品类（产品讲解）", usage="塑品升级细节到不能再细", text="\"你看这台M817的门把手——是金属的、不是塑料的\""),
    ScriptSample(code="04", category="逼单转化类", usage="需求逼单", text="\"你不要觉得你不需要猛士。等哪一天你下班路上下暴雨…\""),
    ScriptSample(code="05", category="逼单转化类", usage="稀缺逼单（合规）", text="\"今天馆里就剩两台现车了…下批28天后到，并且没有现车优惠\""),
    ScriptSample(code="06", category="逼单转化类", usage="痛点逼单", text="\"你家那台城市SUV，上次跑水库直接拖了底。老婆坐车上吓哭了\""),
    ScriptSample(code="07", category="逼单转化类", usage="价值逼单（合规）", text="\"30多万的车，跟你市面那些20万的城市SUV是两个物种\""),
    ScriptSample(code="08", category="逼单转化类", usage="比价话术（合规）", text="\"我不跟别人比价…我们店里这价，今天给你们这一口价\""),
    ScriptSample(code="29", category="逼单转化类", usage="转化收口", text="\"你今天纠结这个车，还是下个月纠结？去年买的人今年涨了2万\""),
    ScriptSample(code="30", category="逼单转化类", usage="信任建立", text="\"我不是在卖给你一台车——我是在卖给你一台能让你家10年不用再换的车\""),
    ScriptSample(code="12", category="互动留人类", usage="留人钩子", text="\"你们先别急着走——我还有三台车没讲到，一台比一台狠\""),
    ScriptSample(code="13", category="互动留人类", usage="场景互动", text="\"现在家里有娃的家人们扣个1，有娃之后你们的座驾是不是都缩水了\""),
    ScriptSample(code="14", category="互动留人类", usage="身份互动", text="\"工地老板打1、越野爱好者打2、宝爸宝妈打3\""),
    ScriptSample(code="15", category="互动留人类", usage="计时互动", text="\"30秒后我下架这台白外黑内的现车。30、29、28——你打'要'我算你的\""),
    ScriptSample(code="25", category="互动留人类", usage="真假问答互动", text="\"你们猜一下落地价多少？猜对了我送一箱油卡\""),
    ScriptSample(code="10", category="人设与品牌类", usage="人设金句", text="\"我之前觉得M817是身份象征；跑了6万公里后明白——它是让你敢带家人去更远地方的车\""),
    ScriptSample(code="17", category="人设与品牌类", usage="心态金句", text="\"小白不是问题，心态才是。错了就错了我改就是了\""),
    ScriptSample(code="21", category="人设与品牌类", usage="草根逆袭故事", text="\"之前开5万块二手车跑工地，陷进去三次，救援费花了8千\""),
    ScriptSample(code="22", category="人设与品牌类", usage="品牌金句（东风语境）", text="\"老一辈开东风卡车，新一辈开东风猛士\""),
    ScriptSample(code="23", category="人设与品牌类", usage="品牌金句（M817专用）", text="\"国有大事，必有猛士\""),
    ScriptSample(code="20", category="答疑与防守类", usage="答疑话术", text="\"涉水深度900mm——就是你老婆孩子的小腿肚。汛期河床没过小腿肚，你照常开过去\""),
    ScriptSample(code="24", category="答疑与防守类", usage="信任建立（实物佐证）", text="\"这是我们上个月才下线的真车，里程36公里\""),
    ScriptSample(code="27", category="答疑与防守类", usage="承接差评", text="\"我不评价别的车。我就告诉你——东风军工底盘60年，你坐进去踩一脚油门去越野试试再说话\""),
    ScriptSample(code="28", category="答疑与防守类", usage="场景话术（救援场）", text="\"你老婆半夜在高速抛锚了…M817有紧急救援模式一键呼叫\""),
    ScriptSample(code="18", category="通用类", usage="破价拒单", text="\"你要是想开一台让你家庭出行不掉链子的车，你就别跟我谈价\""),
)

# 违规避坑词库：直播话术里严禁出现，以及合规替换说法。
VIOLATION_TERMS: tuple[ViolationTerm, ...] = (
    ViolationTerm(term="极致/最/第一/唯一/顶级/销量冠军/全国销量第一", replacement="\"非常\"\"相当\"\"做工扎实\"\"用户口碑\""),
    ViolationTerm(term="绝对/肯定/一定/100%", replacement="\"我们相信\"\"大概率\"\"通常情况下\""),
    ViolationTerm(term="绝对低价/全网最低/全网最划算/比某某家便宜", replacement="\"我们店里这价\"\"今天这一口价\""),
    ViolationTerm(term="最后X件/错过不再有/倒计时几小时", replacement="基于真实库存；否则\"我们正在加急调配\""),
    ViolationTerm(term="30天无理由/7天退（页面不一致的）", replacement="按页面承诺讲，不口头附加新承诺"),
)

# 编号集合：归一时用来判断模型给的编号是否存在（不存在的编号要记账，不能默默留下）
MIND_SET_CODES: frozenset[str] = frozenset(item.code for item in MIND_SETS)
SAMPLE_CODES: frozenset[str] = frozenset(item.code for item in SCRIPT_SAMPLES)


def sample_by_code(code: str) -> ScriptSample | None:
    """按编号取话术样板；编号不存在时返回 None。"""
    return _SAMPLES_BY_CODE.get(code)


_SAMPLES_BY_CODE: dict[str, ScriptSample] = {item.code: item for item in SCRIPT_SAMPLES}


def render_mindset_index() -> str:
    """渲染心法索引：每行「编号｜名称｜检核项」，供提示词注入。

    只给检核项与相关样板编号，不给准则里的「改进」原文——改进动作由模型结合本场实际产出，
    照抄准则里的通用改法正是准则 §二十五 第 11 条要禁止的空话。
    """
    lines: list[str] = []
    for item in MIND_SETS:
        tail = f"（相关话术样板：{'、'.join(item.sample_codes)}）" if item.sample_codes else ""
        lines.append(f"- {item.code}｜{item.name}｜检核：{item.check}{tail}")
    return "\n".join(lines)


def render_script_samples() -> str:
    """渲染话术样板库：按场景分类分组，每行「编号｜用法｜原文」。"""
    lines: list[str] = []
    category = ""
    for item in SCRIPT_SAMPLES:
        if item.category != category:
            category = item.category
            lines.append(f"**{category}**")
        label = f"（{item.usage}）" if item.usage else ""
        lines.append(f"- {item.code}{label}：{item.text}")
    return "\n".join(lines)


def render_violation_terms() -> str:
    """渲染违规词库：每行「违规表达 → 合规替换」。"""
    return "\n".join(
        f"- {item.term} → {item.replacement}" for item in VIOLATION_TERMS
    )


__all__ = [
    "MIND_SETS",
    "MIND_SET_CODES",
    "SAMPLE_CODES",
    "SCRIPT_SAMPLES",
    "VIOLATION_TERMS",
    "Mindset",
    "ScriptSample",
    "ViolationTerm",
    "render_mindset_index",
    "render_script_samples",
    "render_violation_terms",
    "sample_by_code",
]
