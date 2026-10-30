"""方案第 2 版 (docs/api/data-nodes.md): parsing, the platform's tidying, answered
questions, the checks that send a plan back and what the checked plan holds."""

import copy
import dataclasses

import pytest

from aep.etl.plan import (
    ITEM_ID,
    OUTPUT_FORMAT,
    OUTPUT_FORMAT_V2,
    EtlPlan,
    EtlPlanV2,
    KnownValues,
    PlanError,
    answer_output,
    bare,
    uncited,
    validate_plan_v2,
)
from aep.etl.profile import seen_values
from aep.knowledge.citations import IndexedHit
from aep.runtime.flow import nodes

SOURCES = ["sales.orders", "sales.customers"]
SEEN = {
    "sales.customers": {"city": frozenset({"上海", "北京"})},
    # Not all values listed: a time.
    "sales.orders": {"status": frozenset({"paid"}), "ordered_at": None},
}
KNOWN = {"status": KnownValues(frozenset({"paid"}), True)}
HIT = IndexedHit(
    kb_id="kb-1",
    kb_name="零售数据口径",
    document_id="doc-1",
    document_name="01-有效订单定义.md",
    chunk_id="chunk-1",
    position=0,
    text="有效订单是状态为 paid 的订单。",
    heading="有效订单",
    page=None,
    score=0.9,
    index=1,
)


def brief_plan(**changes) -> dict:
    """A version 2 plan: a detail table and a daily result table by city."""
    base = {
        "summary": "按日、按城市统计有效订单。",
        "language": "pyspark",
        "brief": {
            "goal": "按日、按城市统计订单数和商品数量 [1]",
            "grain": "每个统计日 × 城市一行",
            "metrics": [
                {
                    "id": "m1",
                    "name": "订单数",
                    "unit": "单",
                    "definition": "有效订单的个数 [1]",
                    "column": "order_count",
                },
                {
                    "id": "m2",
                    "name": "商品数量",
                    "unit": "件",
                    "definition": "订单里商品数量之和",
                    "column": "quantity_sum",
                },
                {
                    "id": "m3",
                    "name": "平均数量",
                    "definition": "商品数量除以订单数",
                    "column": "avg_quantity",
                },
            ],
            "dimensions": [
                {"id": "d1", "name": "统计日", "definition": "下单日期", "column": "stat_day"},
                {
                    "id": "d2",
                    "name": "城市",
                    "definition": "下单客户档案里的城市",
                    "column": "city",
                    "examples": ["上海", "北京"],
                },
            ],
            "filters": [{"id": "f1", "text": "只算已支付的订单 [1]"}],
            "time_range": {"text": "全部日期，按下单时间", "column": "stat_day"},
        },
        "targets": [
            {
                "name": "order_facts",
                "description": "订单明细，一单一行",
                "sources": ["sales.orders", "sales.customers"],
                "logic": "订单关联客户，只留已支付的订单",
                "columns": [
                    {"name": "order_id", "source": "o.order_id", "label": "订单号"},
                    {
                        "name": "stat_day",
                        "source": "date_format(o.ordered_at, 'yyyy-MM-dd')",
                        "label": "下单日期",
                    },
                    {"name": "city", "source": "coalesce(trim(c.city), '未知')", "label": "城市"},
                    {"name": "quantity", "source": "o.quantity", "label": "数量"},
                ],
                "primary_key": ["order_id"],
                "quality_rules": [
                    {
                        "name": "positive_quantity",
                        "condition": "quantity > 0",
                        "on_violation": "drop",
                    }
                ],
            },
            {
                "name": "city_daily",
                "description": "每天每个城市的订单数和商品数量",
                "sources": ["order_facts"],
                "logic": "按日期和城市汇总",
                "columns": [
                    {"name": "stat_day", "source": "stat_day", "label": "统计日"},
                    {"name": "city", "source": "city", "label": "城市"},
                    {"name": "order_count", "source": "count(order_id)", "label": "订单数（单）"},
                    {"name": "quantity_sum", "source": "sum(quantity)", "label": "商品数量（件）"},
                    {
                        "name": "avg_quantity",
                        "source": "quantity_sum / order_count",
                        "label": "平均数量",
                    },
                ],
                "primary_key": ["stat_day", "city"],
            },
        ],
        "rules": [{"id": "r1", "text": "城市去掉首尾空格", "basis": "knowledge"}],
        "questions": [
            {
                "id": "q1",
                "text": "统计哪段时间？",
                "why": "需求没说时间范围",
                "options": [
                    {"value": "all", "label": "全部日期"},
                    {"value": "y2026", "label": "只看 2026 年"},
                ],
                "suggested": "all",
                "affects": ["m1", "m2"],
            },
            {
                "id": "q2",
                "text": "城市为空时怎么算？",
                "options": [
                    {"value": "unknown", "label": "算作未知"},
                    {"value": "drop", "label": "不算"},
                ],
                "suggested": "unknown",
            },
        ],
        "answer": {
            "target": "city_daily",
            "filter": [{"column": "stat_day", "pick": "latest"}],
            "group_by": ["city"],
            "measures": ["order_count", "quantity_sum", "avg_quantity"],
        },
    }
    base.update(changes)
    return base


def check(raw: dict, **kw) -> tuple[EtlPlanV2, list[str]]:
    args = {"known": KNOWN, "seen": SEEN, "references": [HIT]} | kw
    return validate_plan_v2(raw, SOURCES, **args)


def problems(raw: dict, **kw) -> list[str]:
    with pytest.raises(PlanError) as failed:
        check(raw, **kw)
    return failed.value.problems


def question(qid: str, text: str, *values: str, **changes) -> dict:
    options = [{"value": v, "label": f"选项{v}"} for v in values]
    return {"id": qid, "text": text, "options": options, "suggested": values[0]} | changes


def rule(text: str, basis: str | None = "knowledge", **changes) -> dict:
    return {"text": text, "basis": basis} | changes


def rules(*texts: str) -> list[dict]:
    return [rule(text, id=f"r{i}") for i, text in enumerate(texts, 1)]


def test_a_good_plan_is_a_superset_of_version_one():
    plan, cleaned = check(brief_plan())
    assert cleaned == []
    dumped = plan.model_dump()
    assert EtlPlan.model_validate(dumped).targets[1].name == "city_daily"
    assert dumped["assumptions"] == []
    assert [r["index"] for r in dumped["references"]] == [1]
    assert [c["label"] for c in dumped["targets"][1]["columns"]] == [
        "统计日",
        "城市",
        "订单数（单）",
        "商品数量（件）",
        "平均数量",
    ]
    assert dumped["answer"] == {
        "target": "city_daily",
        "filter": [{"column": "stat_day", "pick": "latest"}],
        "group_by": ["city"],
        "measures": ["order_count", "quantity_sum", "avg_quantity"],
        "order_by": [{"column": "order_count", "desc": True}],
        "top_n": 10,
        "chart": {"type": "bar", "label": ["city"], "value": "order_count"},
    }
    assert dumped["brief"]["metrics"][0] == {
        "id": "m1",
        "name": "订单数",
        "unit": "单",
        "definition": "有效订单的个数 [1]",
        "column": "order_count",
    }
    assert [q["id"] for q in dumped["questions"]] == ["q1", "q2"]
    assert dumped["rules"] == [{"id": "r1", "text": "城市去掉首尾空格", "basis": "knowledge"}]


def test_the_summary_comes_from_the_goal_when_missing():
    raw = brief_plan()
    del raw["summary"]
    plan, _ = check(raw)
    assert plan.summary == "按日、按城市统计订单数和商品数量 [1]"
    plan, _ = check(brief_plan(summary="  "))
    assert plan.summary.startswith("按日、按城市")


def test_version_one_stays_as_it_is():
    assert set(EtlPlan.model_fields) == {
        "summary",
        "language",
        "targets",
        "assumptions",
        "references",
    }
    assert "brief" not in OUTPUT_FORMAT and "assumptions" in OUTPUT_FORMAT
    for key in ('"brief"', '"rules"', '"questions"', '"answer"', '"label"'):
        assert key in OUTPUT_FORMAT_V2


def test_item_ids_follow_the_node_id_rule():
    assert ITEM_ID.pattern == nodes.ID.pattern


# 平台自动整理


def test_a_shared_english_prefix_is_dropped():
    raw = brief_plan(
        rules=rules(
            "advance: 城市去空格",
            "advance：金额保留两位",
            "advance- 数量为空算 0",
            "advance — 只算已支付",
            "note: 只出现一次的不动",
            "GMV: 大写缩写不动",
            "GMV: 大写缩写不动 2",
            "e-commerce 渠道单独算",
            "e-commerce 渠道不含退款",
        )
    )
    raw["brief"]["filters"] = [
        {"id": "f1", "text": "only: 已支付"},
        {"id": "f2", "text": "only: 不是测试账号"},
    ]
    raw["questions"][0]["options"] = [
        {"value": "all", "label": "opt: 全部日期"},
        {"value": "y2026", "label": "opt: 只看 2026 年"},
    ]
    plan, cleaned = check(raw)
    assert [r.text for r in plan.rules] == [
        "城市去空格",
        "金额保留两位",
        "数量为空算 0",
        "只算已支付",
        "note: 只出现一次的不动",
        "GMV: 大写缩写不动",
        "GMV: 大写缩写不动 2",
        "e-commerce 渠道单独算",
        "e-commerce 渠道不含退款",
    ]
    assert plan.brief is not None and [f.text for f in plan.brief.filters] == [
        "已支付",
        "不是测试账号",
    ]
    assert [o.label for o in plan.questions[0].options or []] == ["全部日期", "只看 2026 年"]
    assert cleaned == [
        "去掉了 2 条筛选开头的英文前缀“only:”",
        "去掉了 4 条规则开头的英文前缀“advance:”",
        "去掉了 2 条问题“统计哪段时间？”的选项开头的英文前缀“opt:”",
    ]


def test_numbering_and_prefixes_are_dropped():
    raw = brief_plan(
        rules=rules(
            "1. 城市去空格",
            "2、金额保留两位",
            "(3) 数量为空算 0",
            "④只算已支付",
            "- 不含测试账号",
            "假设：退款不算",
            "1. 规则：运费不算",
            "1.5 倍以上的订单单独看",
        )
    )
    raw["questions"][0]["text"] = "问题：统计哪段时间？"
    plan, cleaned = check(raw)
    assert [r.text for r in plan.rules] == [
        "城市去空格",
        "金额保留两位",
        "数量为空算 0",
        "只算已支付",
        "不含测试账号",
        "退款不算",
        "运费不算",
        "1.5 倍以上的订单单独看",
    ]
    assert plan.questions[0].text == "统计哪段时间？"
    assert cleaned == [
        "去掉了 7 条规则开头的编号或“假设：”这类前缀",
        "去掉了 1 条问题开头的编号或“假设：”这类前缀",
    ]


def test_repeats_are_dropped_but_options_are_not():
    raw = brief_plan(
        rules=rules("城市去掉首尾空格", "城市 去掉首尾空格。", "退款不算"),
    )
    raw["questions"].append(question("q3", "统计哪段时间", "a", "b"))
    raw["questions"][1]["options"] = [
        {"value": "unknown", "label": "算作未知"},
        {"value": "unknown2", "label": "算作未知"},
    ]
    plan, cleaned = check(raw)
    assert [r.text for r in plan.rules] == ["城市去掉首尾空格", "退款不算"]
    assert [q.id for q in plan.questions] == ["q1", "q2"]
    assert len(plan.questions[1].options or []) == 2
    assert cleaned == ["去掉了 1 条重复的规则", "去掉了 1 条重复的问题"]


def test_a_citation_keeps_its_number_when_texts_are_compared():
    second = dataclasses.replace(HIT, index=2, chunk_id="chunk-2")
    raw = brief_plan(
        rules=rules("城市名去掉末尾的市 [1]", "城市名去掉末尾的市 [2]", "城市名去掉末尾的市[1]。")
    )
    plan, cleaned = check(raw, references=[HIT, second])
    assert [r.id for r in plan.rules] == ["r1", "r2"]
    assert [h.index for h in plan.references] == [1, 2]
    assert cleaned == ["去掉了 1 条重复的规则"]
    assert bare("统计哪段时间？[1]") == "统计哪段时间1" != bare("统计哪段时间？")


def test_an_unknown_basis_is_an_assumption_and_old_assumptions_become_rules():
    raw = brief_plan(
        rules=[rule("城市去空格", "glossary"), {"text": "退款不算"}, rule("金额两位", None)],
        assumptions=["status 用 paid"],
    )
    plan, cleaned = check(raw)
    assert [(r.id, r.text, r.basis) for r in plan.rules] == [
        ("r1", "城市去空格", "assumption"),
        ("r2", "退款不算", "assumption"),
        ("r3", "金额两位", "assumption"),
        ("r4", "status 用 paid", "assumption"),
    ]
    assert plan.assumptions == []
    assert "1 条规则的依据 basis 不认识，按 assumption（假设）处理" in cleaned
    assert "给 4 个没写编号的规则编了号：r1、r2、r3、r4" in cleaned


def test_ids_are_renumbered_and_affects_follow():
    raw = brief_plan()
    raw["brief"]["metrics"][0]["id"] = "M-1"
    del raw["brief"]["metrics"][1]["id"]
    raw["rules"][0]["id"] = 3
    raw["questions"][0]["affects"] = ["M-1", "m3", 3, "nope", "q2", "q9"]
    plan, cleaned = check(raw)
    assert plan.brief is not None
    assert [m.id for m in plan.brief.metrics] == ["m1", "m2", "m3"]
    assert plan.rules[0].id == "r1"
    # Only ids that name nothing go: q2 is a question of the plan.
    assert plan.questions[0].affects == ["m1", "m3", "r1", "q2"]
    assert cleaned == [
        "指标的编号“M-1”写法不对，改成了 m1",
        "给 1 个没写编号的指标编了号：m2",
        "规则的编号“3”写法不对，改成了 r1",
        "去掉了问题 q1 的 affects 里不存在的 nope、q9",
    ]


def test_duplicate_ids_go_back():
    raw = brief_plan()
    raw["rules"][0]["id"] = "m1"
    raw["questions"][1]["id"] = "q1"
    assert problems(raw) == [
        "id m1 重复（brief.metrics[0]、rules[0]），指标、维度、筛选、规则和问题的 id 不能重复",
        "id q1 重复（questions[0]、questions[1]），指标、维度、筛选、规则和问题的 id 不能重复",
    ]


def test_question_values_why_and_affects_are_tidied():
    raw = brief_plan()
    first = raw["questions"][0]
    first["options"] = [{"value": 2026, "label": "只看 2026 年"}, {"value": True, "label": "全部"}]
    first["suggested"] = 2026
    first["why"] = "长" * 600
    first["affects"] = ["m1", "m2", "m1"] + [f"x{i}" for i in range(22)]
    raw["rules"] = [rule(f"规则{i}", id=f"x{i}") for i in range(22)]
    plan, cleaned = check(raw)
    q = plan.questions[0]
    assert [o.value for o in q.options or []] == ["2026", "true"]
    assert q.suggested == "2026"
    assert q.why == "长" * 500
    assert q.affects == ["m1", "m2", *(f"x{i}" for i in range(18))]
    assert cleaned == [
        "问题 q1 的选项值或建议答案从数字或 true、false 转成了文字",
        "问题 q1 的“为什么问”超过 500 个字符，截断了",
        "问题 q1 的 affects 去掉了重复的",
        "问题 q1 的 affects 只留了前 20 个",
    ]


def test_a_question_with_fewer_than_two_options_becomes_a_rule():
    raw = brief_plan()
    raw["questions"] += [
        {"id": "q3", "text": "退款算不算？", "options": [{"value": "no", "label": "不算"}]},
        {"id": "q4", "text": "运费算不算？", "options": [{"value": "no"}]},
        {"id": "q5", "text": "测试账号算不算？", "options": []},
        {"id": "q6", "text": "赠品算不算？"},
    ]
    plan, cleaned = check(raw)
    assert [q.id for q in plan.questions] == ["q1", "q2"]
    assert [(r.id, r.text, r.basis) for r in plan.rules[1:]] == [
        ("r2", "退款算不算？——按「不算」处理", "assumption"),
        ("r3", "运费算不算？", "assumption"),  # an option's value is not its name
        ("r4", "测试账号算不算？", "assumption"),
        ("r5", "赠品算不算？", "assumption"),
    ]
    assert cleaned == [
        "问题“退款算不算？”的选项少于 2 个，改成了规则 r2",
        "问题“运费算不算？”的选项少于 2 个，改成了规则 r3",
        "问题“测试账号算不算？”的选项少于 2 个，改成了规则 r4",
        "问题“赠品算不算？”的选项少于 2 个，改成了规则 r5",
    ]


def test_questions_from_the_sixth_on_become_rules():
    many = [question(f"q{i}", f"第 {i} 个问题？", "a", "b") for i in range(1, 6)]
    sixth = question("q6", "第 6 个问题？", "a", "b", suggested="b")
    seventh = {
        "id": "q7",
        "text": "第 7 个问题？",
        "options": [{"value": str(i)} for i in range(8)],
    }
    plan, cleaned = check(brief_plan(questions=[*many, sixth, seventh]))
    assert [q.id for q in plan.questions] == ["q1", "q2", "q3", "q4", "q5"]
    assert [(r.id, r.text) for r in plan.rules[1:]] == [
        ("r2", "第 6 个问题？——按「选项b」处理"),
        ("r3", "第 7 个问题？"),
    ]
    assert cleaned == ["问题最多 5 个，第 6 题起的 2 题按建议的选项改成了规则 r2、r3"]


def test_a_question_made_a_rule_is_checked_for_code_where_the_model_wrote_it():
    raw = brief_plan()
    raw["questions"] += [
        {"id": "q3", "text": "城市怎么归一？", "options": [{"value": "trim(city)"}]},
        {"id": "q4", "text": "空值怎么算？", "options": [{"value": "a", "label": "按lower(城市)"}]},
        {"id": "q5", "text": "按count(订单)算吗？"},
    ]
    found = problems(raw)
    assert [p.split(" 里有代码")[0] for p in found] == [
        "questions[3].options[0].label",
        "questions[4].text",
    ]
    many = [question(f"q{i}", f"第 {i} 个问题？", "a", "b") for i in range(1, 6)]
    sixth = question("q6", "第 6 个问题？", "a", "b", suggested="b")
    sixth["options"][1]["label"] = "按coalesce(空值)"
    found = problems(brief_plan(questions=[*many, sixth]))
    assert [p.split(" 里有代码")[0] for p in found] == ["questions[5].options[1].label"]


def test_examples_keep_only_values_in_the_data():
    raw = brief_plan()
    city = raw["brief"]["dimensions"][1]
    city["examples"] = [" 上海 ", "未知", "广州", 3, "北京"]
    plan, cleaned = check(raw)
    assert plan.brief is not None
    assert plan.brief.dimensions[1].examples == [" 上海 ", "未知", "北京"]
    assert cleaned == ["维度“城市”的例子去掉了探查里没见过的 广州、3"]

    raw = brief_plan()
    raw["brief"]["dimensions"][1]["examples"] = ["上海", "北京", "未知", "上海"]
    raw["brief"]["dimensions"][0]["examples"] = ["2026-10-01"]
    raw["brief"]["dimensions"].append(
        {"id": "d3", "name": "渠道", "definition": "下单渠道", "examples": ["线上"]}
    )
    plan, cleaned = check(raw)
    assert plan.brief is not None
    # The profile cannot tell for the day (ordered_at's values were not all listed) or for
    # a dimension without a result column: those examples stay as written.
    assert [d.examples for d in plan.brief.dimensions] == [
        ["2026-10-01"],
        ["上海", "北京", "未知", "上海"],
        ["线上"],
    ]
    assert cleaned == []
    raw["brief"]["dimensions"][1]["examples"] = ["上海", "北京", "未知"] * 3
    plan, cleaned = check(raw)
    assert plan.brief is not None and len(plan.brief.dimensions[1].examples or []) == 5
    assert cleaned == ["维度“城市”的例子只留前 5 个"]
    # A column the profile does not know at all: kept too.
    raw = brief_plan()
    raw["brief"]["dimensions"][1]["examples"] = ["广州"]
    plan, cleaned = check(raw, seen={})
    assert plan.brief is not None and plan.brief.dimensions[1].examples == ["广州"]
    assert cleaned == []
    # Examples the model did not give stay unwritten.
    plan, _ = check(brief_plan())
    assert plan.brief is not None and plan.brief.dimensions[0].examples is None


def test_examples_come_from_the_source_columns_the_column_reads():
    """Only the tables the target reads count, an alias or a table name picks its table,
    and function names, keywords and types are no columns."""
    seen = {
        "sales.customers": {"city": frozenset({"上海"}), "upper": frozenset({"苏州"})},
        "sales.orders": {"city": frozenset({"杭州"}), "string": frozenset({"南京"})},
        "sales.stores": {"city": frozenset({"广州"})},
    }
    raw = brief_plan()
    raw["brief"]["dimensions"][1]["examples"] = ["上海", "杭州", "广州", "苏州", "南京", "未知"]
    city = raw["targets"][0]["columns"][2]
    kept = {
        "CASE WHEN c.city IS NULL THEN '未知' ELSE upper(cast(c.city AS string)) END": [
            "上海",
            "未知",
        ],
        "trim(`sales`.`customers`.`city`)": ["上海"],
        "coalesce(trim(orders.city), '未知')": ["杭州", "未知"],
        # Without a qualifier: every table the target reads.
        "coalesce(trim(city), '未知')": ["上海", "杭州", "未知"],
    }
    for source, examples in kept.items():
        city["source"] = source
        plan, _ = check(raw, seen=seen)
        assert plan.brief is not None and plan.brief.dimensions[1].examples == examples, source


def test_seen_values_are_kept_per_table():
    """Only columns whose values were all listed: none cut short, as many as the distinct
    count. The rest are None."""
    tables = [
        {
            "name": "sales.customers",
            "column_stats": [
                {
                    "name": "City",
                    "distinct_count": 2,
                    "top_values": [{"value": " 上海 ", "count": 2}, {"value": "北京", "count": 1}],
                },
                {
                    "name": "level",
                    "distinct_count": 2,
                    "top_values": [
                        {"value": "gold", "count": 2},
                        {"value": "sil…", "count": 1, "truncated": True},
                    ],
                },
                {"name": "registered_at", "distinct_count": 1000},
                {"name": "email", "distinct_count": 3, "top_values": [{"value": "a", "count": 1}]},
            ],
            "sample": [{"city": "杭州", "email": None}],
        },
        {
            "name": "sales.stores",
            "column_stats": [
                {"name": "city", "distinct_count": 1, "top_values": [{"value": "杭州"}]}
            ],
        },
    ]
    assert seen_values(tables) == {
        "sales.customers": {
            "city": frozenset({"上海", "北京"}),
            "level": None,
            "registered_at": None,
            "email": None,
        },
        "sales.stores": {"city": frozenset({"杭州"})},
    }


def test_without_a_brief_the_platform_writes_one_from_the_result_table():
    raw = brief_plan(rules=[rule("城市去空格", id="m1")])
    del raw["brief"], raw["answer"]
    for column in raw["targets"][1]["columns"]:
        column.pop("label")
    raw["targets"][1]["columns"][1]["label"] = "城市"
    raw["targets"][1]["columns"][2]["description"] = "count(order_id) 订单数"
    raw["questions"][0]["affects"] = []
    plan, cleaned = check(raw)
    brief = plan.brief
    assert brief is not None
    assert brief.goal == plan.summary
    assert brief.grain == "每个stat_day × 城市一行"
    assert [(d.id, d.name, d.column) for d in brief.dimensions] == [
        ("d1", "stat_day", "stat_day"),
        ("d2", "城市", "city"),
    ]
    assert [(m.id, m.name, m.column, m.unit) for m in brief.metrics] == [
        ("m2", "order_count", "order_count", None),
        ("m3", "quantity_sum", "quantity_sum", None),
        ("m4", "avg_quantity", "avg_quantity", None),
    ]
    assert brief.metrics[0].definition == "count(order_id) 订单数"
    assert brief.filters == [] and brief.time_range is None
    # The generated answer: no time range, so no filter; the first dimensions as groups.
    assert plan.answer is not None
    assert plan.answer.filter == [] and plan.answer.group_by == ["stat_day", "city"]
    assert plan.answer.measures == ["order_count", "quantity_sum", "avg_quantity"]
    assert cleaned[0] == (
        "模型没写业务说明，平台按结果表 city_daily 的列生成了一份（主键列当维度，其余列当指标）"
    )
    assert "给 city_daily 的 4 列补了显示名称 label" in cleaned
    assert cleaned[-1] == "模型没写答案设计，平台按业务说明生成了一份"


def test_a_generated_brief_without_a_primary_key_has_metrics_only():
    raw = brief_plan()
    del raw["brief"], raw["answer"]
    raw["targets"][1]["primary_key"] = []
    raw["questions"][0]["affects"] = None
    plan, _ = check(raw)
    assert plan.brief is not None and plan.brief.grain is None
    assert plan.brief.dimensions == [] and len(plan.brief.metrics) == 5
    assert plan.answer is not None and plan.answer.group_by == []
    assert plan.answer.chart is not None and plan.answer.chart.type == "none"


def test_labels_and_the_answer_come_from_the_brief():
    raw = brief_plan()
    del raw["answer"]
    for column in raw["targets"][0]["columns"] + raw["targets"][1]["columns"]:
        column.pop("label")
    raw["brief"]["dimensions"].insert(
        0, {"id": "d0", "name": "渠道", "definition": "下单渠道", "column": "channel"}
    )
    raw["targets"][1]["columns"].append({"name": "channel", "source": "'线上'"})
    raw["targets"][1]["primary_key"].append("channel")
    plan, cleaned = check(raw)
    assert [c.label for c in plan.targets[1].columns] == [
        "统计日",
        "城市",
        "订单数（单）",
        "商品数量（件）",
        "平均数量",
        "渠道",
    ]
    assert [c.label for c in plan.targets[0].columns] == [
        "order_id",
        "stat_day",
        "city",
        "quantity",
    ]
    assert plan.answer is not None
    assert plan.answer.model_dump() == {
        "target": "city_daily",
        "filter": [{"column": "stat_day", "pick": "latest"}],
        "group_by": ["channel", "city"],
        "measures": ["order_count", "quantity_sum", "avg_quantity"],
        "order_by": [{"column": "order_count", "desc": True}],
        "top_n": 10,
        "chart": {"type": "bar", "label": ["channel", "city"], "value": "order_count"},
    }
    assert cleaned == [
        "给 order_facts 的 4 列补了显示名称 label",
        "给 city_daily 的 6 列补了显示名称 label",
        "模型没写答案设计，平台按业务说明生成了一份",
    ]


def test_a_brief_without_metrics_has_no_answer():
    raw = brief_plan()
    del raw["answer"]
    raw["brief"]["metrics"] = None
    raw["questions"][0]["affects"] = None
    plan, cleaned = check(raw)
    assert plan.answer is None and answer_output(plan) is None
    assert cleaned == []


# Answered questions


def answered(*items: tuple[str, str, str | None]) -> dict:
    return {
        bare(label): {"label": label, "value_label": value, "actor": actor}
        for label, value, actor in items
    }


def test_answered_questions_become_confirmed_rules_by_their_text():
    raw = brief_plan()
    raw["questions"] = [
        question("q1", "城市为空时怎么算？", "unknown", "drop"),
        question("q2", "统计哪段时间？", "all", "y2026"),
    ]
    memory = answered(("统计哪段时间", "只看 2026 年", "韩梅梅"))
    plan, cleaned = check(raw, answered=memory)
    assert [q.id for q in plan.questions] == ["q1"]
    assert [(r.id, r.text, r.basis) for r in plan.rules] == [
        ("r1", "城市去掉首尾空格", "knowledge"),
        ("r2", "韩梅梅已确认：统计哪段时间——只看 2026 年", "confirmed"),
    ]
    assert cleaned == []
    plan, _ = check(raw, answered=answered(("统计哪段时间？", "只看 2026 年", None)))
    assert plan.rules[-1].text == "已确认：统计哪段时间？——只看 2026 年"


def test_a_confirmed_rule_the_plan_has_is_not_added_twice():
    raw = brief_plan(
        rules=[rule("韩梅梅已确认：统计哪段时间？——只看 2026 年。", "confirmed", id="r7")]
    )
    memory = answered(("统计哪段时间？", "只看 2026 年", "韩梅梅"))
    plan, _ = check(raw, answered=memory)
    assert [q.id for q in plan.questions] == ["q2"]
    assert [r.id for r in plan.rules] == ["r7"]


def test_answered_questions_go_before_the_sixth_becomes_a_rule():
    many = [question(f"q{i}", f"第 {i} 个问题？", "a", "b") for i in range(1, 7)]
    plan, cleaned = check(
        brief_plan(questions=many), answered=answered(("第 3 个问题？", "选项a", "李雷"))
    )
    assert [q.id for q in plan.questions] == ["q1", "q2", "q4", "q5", "q6"]
    assert plan.rules[-1].text == "李雷已确认：第 3 个问题？——选项a"
    assert cleaned == []


def test_a_confirmed_rule_is_not_checked_for_code():
    memory = answered(("统计哪段时间？", "只看 2026 年", "`bob`"))
    plan, _ = check(brief_plan(), answered=memory)
    assert plan.rules[-1].text == "`bob`已确认：统计哪段时间？——只看 2026 年"


# Checks that send the plan back


@pytest.mark.parametrize(
    "text",
    [
        "按count(订单)统计",
        "按sum(金额)",
        "`city` 去空格",
        '按 col("x") 算',
        "F.sum(x)",
        "case when 状态是已支付",
        "Lower(城市)",
        "date_format(下单时间)",
        "CASE\nWHEN 退款",
    ],
)
def test_code_in_business_words_goes_back(text):
    raw = brief_plan(rules=[rule(text)])
    found = problems(raw)
    assert len(found) == 1 and found[0].startswith("rules[0].text 里有代码（")
    assert found[0].endswith("：业务说明给人看，用文字写，表达式写在目标表的列里")


@pytest.mark.parametrize(
    "text",
    [
        "GMV(含税)",
        "销售额（含税）",
        "sum（数量）",
        "Discount(折扣)",
        "summary(总结)",
        "count 订单",
        "Count (订单)",
        "showcase whenever",
        "按 count[1] 计",
        "Dis[1]count(折扣)",
    ],
)
def test_words_that_only_look_like_code_pass(text):
    check(brief_plan(rules=[rule(text)]))


def test_pyspark_in_a_source_or_a_condition_goes_back():
    raw = brief_plan()
    facts, daily = raw["targets"]
    facts["columns"][3]["source"] = 'F.col("o.quantity").cast("int")'
    facts["quality_rules"][0]["condition"] = "col('quantity') > 0"
    daily["columns"][3]["source"] = "sum(quantity).alias('quantity_sum')"
    found = [p for p in problems(raw) if "PySpark" in p]
    assert found == [
        "targets[0] 的 quantity、positive_quantity 写成了 PySpark（F.col(）：列的 source 和"
        "质量规则的 condition 要写 Spark SQL 表达式，如 sum(quantity)、count(order_id)",
        "targets[1] 的 quantity_sum 写成了 PySpark（.alias(）：列的 source 和"
        "质量规则的 condition 要写 Spark SQL 表达式，如 sum(quantity)、count(order_id)",
    ]
    # Spark SQL, an alias f and the same words inside a string literal are fine.
    raw = brief_plan()
    facts, daily = raw["targets"]
    facts["columns"][3]["source"] = "cast(o.quantity AS int)"
    facts["columns"][2]["source"] = "coalesce(trim(c.city), 'F.col(x)')"
    daily["columns"][3]["source"] = "sum(f.quantity)"
    daily["sources"] = ["order_facts"]
    assert not [p for p in problems_or_none(raw) if "PySpark" in p]


def problems_or_none(raw: dict) -> list[str]:
    try:
        check(raw)
    except PlanError as exc:
        return exc.problems
    return []


def test_code_is_looked_for_in_every_business_text():
    raw = brief_plan()
    brief = raw["brief"]
    brief["goal"] = "按`city`统计"
    brief["grain"] = "每个date_trunc(日)一行"
    metric, dimension = brief["metrics"][0], brief["dimensions"][1]
    metric |= {"name": "count(订单)", "definition": "按count(order_id)算 [1]", "unit": "sum(单)"}
    dimension |= {"name": "trim(城市)", "definition": "客户档案里的lower(城市)"}
    brief["filters"][0]["text"] = "CASE WHEN 已支付"
    brief["time_range"]["text"] = "to_date(下单时间)"
    raw["rules"][0]["text"] = "城市nvl(空值)"
    first = raw["questions"][0]
    first["text"] = "按avg(订单)统计哪段时间？"
    first["why"] = "要不要 coalesce(空值)"
    first["options"][1]["label"] = "cast(2026)"
    raw["targets"][1]["columns"][4]["label"] = "avg(数量)"
    # Not business words: the column, the examples and the target's expressions.
    raw["targets"][0]["columns"][2]["source"] = "coalesce(trim(c.city), 'upper(未知)')"
    dimension["examples"] = ["upper(未知)"]
    found = problems(raw)
    assert [p.split(" 里有代码")[0] for p in found] == [
        "brief.goal",
        "brief.grain",
        "brief.metrics[0].name",
        "brief.metrics[0].definition",
        "brief.metrics[0].unit",
        "brief.dimensions[1].name",
        "brief.dimensions[1].definition",
        "brief.filters[0].text",
        "brief.time_range.text",
        "rules[0].text",
        "questions[0].text",
        "questions[0].why",
        "questions[0].options[1].label",
        "targets[1].columns[4].label",
    ]
    assert "（`）" in found[0] and "（count(）" in found[3] and "（avg(）" in found[-1]
    raw = brief_plan()
    raw["targets"][0]["columns"][2]["source"] = "coalesce(trim(c.city), 'upper(未知)')"
    raw["brief"]["dimensions"][1]["examples"] = ["upper(未知)"]
    plan, _ = check(raw)
    assert plan.brief is not None and plan.brief.dimensions[1].examples == ["upper(未知)"]


def test_a_generated_brief_and_filled_labels_are_not_checked_for_code():
    raw = brief_plan()
    del raw["brief"], raw["answer"]
    raw["questions"][0]["affects"] = []
    columns = raw["targets"][1]["columns"]
    columns[3] = {"name": "quantity_sum", "source": "sum(quantity)", "description": "sum(quantity)"}
    plan, _ = check(raw)
    assert plan.brief is not None and plan.brief.metrics[1].definition == "sum(quantity)"
    assert plan.targets[1].columns[3].label == "quantity_sum"


def test_brief_columns_must_be_result_columns():
    raw = brief_plan()
    raw["brief"]["metrics"][1]["column"] = "quantity"
    raw["brief"]["dimensions"][0]["column"] = "order_day"
    raw["brief"]["time_range"]["column"] = "ordered_at"
    found = problems(raw)
    assert found == [
        "brief.metrics[1].column quantity 不是结果表 city_daily 的列",
        "brief.dimensions[0].column order_day 不是结果表 city_daily 的列",
        "brief.time_range.column ordered_at 不是结果表 city_daily 的列",
    ]


def test_question_rules_go_back():
    raw = brief_plan()
    raw["questions"] = [
        question("q1", "选项太多？", *"abcdefg"),
        {
            "id": "q2",
            "text": "选项不对？",
            "options": [
                {"value": "a", "label": "甲"},
                {"value": "a", "label": "乙"},
                {"value": "x" * 65, "label": "丙"},
                {"value": "  ", "label": "丁"},
                {"value": None, "label": "戊"},
                {"value": "b", "label": ""},
            ],
            "suggested": "c",
        },
        question("q3", "1. ", "a", "b"),
        question("q4", "长" * 51, "a", "b"),
        {
            "id": "q5",
            "text": "名称太长？",
            "options": [{"value": "a", "label": "长" * 51}, {"value": "b"}],
        },
    ]
    found = problems(raw)
    assert found == [
        "questions[0].options 最多 6 个",
        "questions[1].options[1].value a 重复",
        "questions[1].options[2].value 要是 1–64 个字符的文字",
        "questions[1].options[3].value 要是 1–64 个字符的文字",
        "questions[1].options[4].value 要是 1–64 个字符的文字",
        "questions[1].options[5].label 不能为空",
        "questions[1].suggested c 不在选项里",
        "questions[2].text 不能为空",
        "questions[3].text 最多 50 个字符，解释写在 why 里",
        "questions[4].options[0].label 最多 50 个字符",
        "questions[4].options[1].label 不能为空",
        "questions[4].suggested 要写方案现在按哪个选项做（某个选项的 value）",
    ]


def test_a_cited_label_that_comes_out_empty_goes_back():
    raw = brief_plan()
    raw["questions"][0]["options"][0]["label"] = "[7]"
    assert problems(raw) == ["questions[0].options[0].label 不能为空"]


def test_answer_rules_go_back_with_the_last_target_as_the_result_table():
    raw = brief_plan()
    raw["answer"] = {"target": "nope", "measures": ["order_count"], "top_n": 80}
    raw["brief"]["metrics"][0]["column"] = "order_id"
    found = problems(raw)
    assert found == [
        "brief.metrics[0].column order_id 不是结果表 city_daily 的列",
        "answer.target nope 不是目标表",
        "answer.top_n 要在 1–50 之间",
    ]
    raw["targets"].reverse()
    raw["targets"][1]["sources"] = SOURCES
    raw["targets"][0]["sources"] = ["order_facts"]
    found = problems(raw)
    assert "brief.metrics[1].column quantity_sum 不是结果表 order_facts 的列" in found
    assert "answer.measures 的列 order_count 不在结果表 order_facts 里" in found


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda p: p["brief"]["metrics"][1].pop("name"), ["brief.metrics[1].name 不能为空"]),
        (lambda p: p["brief"]["metrics"][1].update(name=None), ["brief.metrics[1].name 不能为空"]),
        (lambda p: p["rules"][0].update(text=None), ["rules[0].text 不能为空"]),
        (lambda p: p["brief"].update(goal=3), ["brief.goal 应该是文字"]),
        (lambda p: p["brief"].update(goal=" "), ["brief.goal 不能为空"]),
        (lambda p: p["brief"]["time_range"].update(text=""), ["brief.time_range.text 不能为空"]),
        (lambda p: p.update(rules="城市去空格"), ["rules 应该是列表"]),
        (lambda p: p["questions"][0].update(options="全部"), ["questions[0].options 应该是列表"]),
        (lambda p: p["questions"][0].pop("text"), ["questions[0].text 不能为空"]),
        (lambda p: p.update(answer="city_daily"), ["answer 应该是对象"]),
        (lambda p: p["answer"].pop("measures"), ["answer.measures 不能为空"]),
        (lambda p: p["answer"].update(top_n=True), ["answer.top_n 应该是整数"]),
        (lambda p: p["answer"].update(top_n="10"), ["answer.top_n 应该是整数"]),
        (lambda p: p.update(assumptions=["a", 2]), ["assumptions[1] 应该是文字"]),
        (lambda p: p.update(assumptions="a"), ["assumptions 应该是列表"]),
        (
            lambda p: p["targets"][1]["columns"][0].update(label=3),
            ["targets[1].columns[0].label 应该是文字"],
        ),
        (
            lambda p: p["targets"][0].update(load={"mode": "daily"}),
            ["targets[0].load.mode 只能是 full、incremental"],
        ),
        (lambda p: p.update(targets=[]), ["targets 至少要有 1 项"]),
    ],
)
def test_structure_problems_say_where(change, expected):
    raw = brief_plan()
    change(raw)
    assert problems(raw) == expected


def no_summary_and(goal: str):
    def change(raw: dict) -> None:
        del raw["summary"]
        raw["brief"]["goal"] = goal

    return change


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda p: p.update(summary="[9]"), ["summary 不能为空"]),
        (no_summary_and("[9]"), ["summary 不能为空", "brief.goal 不能为空"]),
        (
            lambda p: p["brief"]["metrics"][1].update(definition="[9]"),
            ["brief.metrics[1].definition 不能为空"],
        ),
        (
            lambda p: p["brief"]["dimensions"][0].update(definition=" [9] "),
            ["brief.dimensions[0].definition 不能为空"],
        ),
        (lambda p: p["brief"]["time_range"].update(text="[9]"), ["brief.time_range.text 不能为空"]),
        (lambda p: p["brief"]["filters"][0].update(text="[9]"), ["brief.filters[0].text 不能为空"]),
        (
            lambda p: p["brief"]["filters"][0].update(text="说明："),
            ["brief.filters[0].text 不能为空"],
        ),
        (lambda p: p["rules"][0].update(text="规则："), ["rules[0].text 不能为空"]),
        (lambda p: p["rules"][0].update(text="[9]"), ["rules[0].text 不能为空"]),
        (lambda p: p["questions"][1].update(text="[9]"), ["questions[1].text 不能为空"]),
    ],
)
def test_a_text_that_comes_out_empty_goes_back(change, expected):
    """Blank is judged after the platform's tidying: a text of an invalid [n] or of a
    prefix alone says nothing."""
    raw = brief_plan()
    change(raw)
    assert problems(raw) == expected


def test_without_a_summary_or_goal_the_plan_goes_back():
    raw = brief_plan()
    del raw["summary"], raw["brief"]
    assert problems(raw) == ["summary 不能为空"]
    assert problems(brief_plan(summary=" ", brief=None)) == ["summary 不能为空"]
    assert problems(brief_plan(summary=None, brief=None)) == ["summary 不能为空"]


def test_null_sections_are_left_out():
    raw = brief_plan(rules=None, questions=None)
    raw["brief"]["time_range"] = None
    raw["brief"]["filters"] = None
    plan, _ = check(raw)
    assert plan.rules == [] and plan.questions == []
    assert plan.brief is not None and plan.brief.time_range is None


def test_a_broken_section_is_not_generated_and_the_rest_is_still_checked():
    raw = brief_plan(answer="city_daily", rules=[rule("按sum(金额)")])
    found = problems(raw)
    assert found == [
        "answer 应该是对象",
        "rules[0].text 里有代码（sum(）：业务说明给人看，用文字写，表达式写在目标表的列里",
    ]
    raw = brief_plan(brief={"goal": "x", "metrics": "订单数"})
    assert problems(raw) == ["brief.metrics 应该是列表"]
    # The core of the plan stops everything else.
    raw = brief_plan(rules=[rule("按sum(金额)")])
    raw["targets"][0]["columns"][0].pop("source")
    assert problems(raw) == ["targets[0].columns[0].source 不能为空"]
    with pytest.raises(PlanError, match="方案要是一个 JSON 对象"):
        check([])  # type: ignore[arg-type]


def test_version_one_checks_run_too():
    raw = brief_plan()
    raw["targets"][0]["sources"] = ["sales.missing"]
    assert problems(raw) == ["order_facts 引用了不存在的源表 'sales.missing'"]


# References and the value check's escape hatch


def test_citations_count_in_every_version_two_text():
    places = ("goal", "grain", "time_range", "filter", "question", "definition", "rule", "why")
    for place in (*places, "label"):
        raw = brief_plan(summary="统计订单。")
        raw["brief"]["goal"] = "统计订单"
        raw["brief"]["metrics"][0]["definition"] = "有效订单的个数"
        raw["brief"]["filters"][0]["text"] = "只算已支付 [7]"
        if place == "goal":
            raw["brief"]["goal"] = "统计订单 [1][7]"
        elif place == "grain":
            raw["brief"]["grain"] = "每个统计日 × 城市一行 [1][7]"
        elif place == "time_range":
            raw["brief"]["time_range"]["text"] = "全部日期 [1][7]"
        elif place == "filter":
            raw["brief"]["filters"][0]["text"] = "只算已支付 [1][7]"
        elif place == "question":
            raw["questions"][0]["text"] = "统计哪段时间？[1][7]"
        elif place == "definition":
            raw["brief"]["metrics"][1]["definition"] = "数量之和 [1][7]"
        elif place == "rule":
            raw["rules"][0]["text"] = "城市去空格 [1] [7]"
        elif place == "why":
            raw["questions"][0]["why"] = "需求没说 [1][7]"
        else:
            raw["questions"][1]["options"][0]["label"] = "算作未知 [1][7]"
        plan, _ = check(raw)
        assert [r.index for r in plan.references] == [1], place
        assert "[7]" not in plan.model_dump_json()
    plan, _ = check(raw)
    assert uncited(plan) == ["m1", "m2", "m3", "d1", "d2", "f1"]
    plan, _ = check(brief_plan())
    assert uncited(plan) == ["m2", "m3", "d1", "d2"]


def refunds(**changes) -> dict:
    raw = brief_plan(**changes)
    raw["targets"][0]["columns"].append(
        {"name": "refunded", "source": "CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END"}
    )
    return raw


def test_a_value_the_data_lacks_passes_once_rules_or_questions_name_it():
    assert any("'refunded'" in p for p in problems(refunds(), trust_assumptions=False))
    assert any("'refunded'" in p for p in problems(refunds(), trust_assumptions=True))
    named = [
        refunds(rules=[rule("退款状态记作 refunded，数据里暂时没有", "assumption")]),
        refunds(assumptions=["status 为 refunded 表示退款"]),
    ]
    raw = refunds()
    raw["questions"][0]["why"] = "数据里没有 refunded"
    named.append(raw)
    raw = refunds()
    raw["questions"][1]["options"][1]["label"] = "refunded 不算"
    named.append(raw)
    for raw in named:
        assert any("'refunded'" in p for p in problems(copy.deepcopy(raw), trust_assumptions=False))
        check(raw, trust_assumptions=True)
    raw = refunds()
    raw["brief"]["metrics"][0]["definition"] = "不含 refunded 的订单"
    assert any("'refunded'" in p for p in problems(raw, trust_assumptions=True))


def test_the_answer_output_has_columns_and_a_sentence():
    plan, _ = check(brief_plan())
    out = answer_output(plan)
    assert out is not None
    assert out["columns"] == [
        {"name": "city", "label": "城市", "role": "dimension"},
        {
            "name": "order_count",
            "label": "订单数（单）",
            "role": "metric",
            "agg": "sum",
            "unit": "单",
        },
        {
            "name": "quantity_sum",
            "label": "商品数量（件）",
            "role": "metric",
            "agg": "sum",
            "unit": "件",
        },
        {
            "name": "avg_quantity",
            "label": "平均数量",
            "role": "metric",
            "agg": "ratio",
            "unit": None,
            "numerator": "quantity_sum",
            "denominator": "order_count",
        },
    ]
    assert (
        out["text"]
        == "最近一个统计日里，各「城市」组合按订单数从高到低排的前 10 组，附合计和柱状图。"
    )
    assert list(out) == [
        "target",
        "filter",
        "group_by",
        "measures",
        "order_by",
        "top_n",
        "chart",
        "columns",
        "text",
    ]
