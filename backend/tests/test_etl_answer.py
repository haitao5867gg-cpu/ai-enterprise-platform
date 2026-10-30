"""The answer design of a version 2 plan (docs/api/data-nodes.md, "答案设计和可加性"):
which measures add up across groups, the design's defaults and rules, its columns and
the sentence the plan review shows."""

import pytest

from aep.etl.answer import (
    AnswerDesign,
    additivity,
    columns,
    design_problems,
    generate,
    text,
    with_defaults,
)

# The result table of the contract's example.
SOURCES = {
    "stat_month": "date_format(order_date, 'yyyy-MM')",
    "city": "city",
    "member_level": "member_level",
    "sales_amount": "SUM(`line_amount`)",
    "order_count": "count(order_id)",
    "customer_count": "count(DISTINCT customer_id)",
    "avg_order_value": "round(sales_amount / order_count, 2)",
}
LABELS = {
    "stat_month": "统计月",
    "city": "城市",
    "member_level": "会员等级",
    "sales_amount": "销售额（元）",
    "order_count": "订单数",
    "customer_count": "下单客户数",
    "avg_order_value": "客单价（元）",
}
TARGETS = {"ads_sales_city_member_m": list(SOURCES), "order_facts": ["order_id", "city"]}


def design(**changes) -> AnswerDesign:
    base = {
        "target": "ads_sales_city_member_m",
        "filter": [{"column": "stat_month", "pick": "latest"}],
        "group_by": ["city", "member_level"],
        "measures": ["sales_amount", "order_count", "customer_count", "avg_order_value"],
    }
    return AnswerDesign.model_validate(base | changes)


@pytest.mark.parametrize(
    "source",
    [
        "sum(line_amount)",
        "SUM(oi.quantity*oi.unit_price)",
        "count(*)",
        "count(1)",
        "count(`o`.`order_id`)",
        "count_if(status = 'paid')",
        "round(sum(x), 2)",
        "bround(sum(x), 2)",
        "round(sum(x))",
        "cast(sum(x) AS DECIMAL(18, 2))",
        "try_cast(count(*) AS BIGINT)",
        "coalesce(sum(x), 0)",
        "nvl(count(id), 0)",
        "ifnull(sum(x), 0.0)",
        "round(coalesce(sum(x),0),2)",
        "(sum(x))",
        "  sum (x) ",
    ],
)
def test_sums_and_counts_add_up(source):
    assert additivity("m", {"m": source}) == ("sum", None, None)


@pytest.mark.parametrize(
    "source",
    [
        "coalesce(sum(x), 1)",
        "coalesce(sum(x),0,1)",
        "count(DISTINCT order_id)",
        "sum(distinct x)",
        "avg(x)",
        "min(x)",
        "max(x)",
        "approx_count_distinct(customer_id)",
        "sum(x) OVER (PARTITION BY y)",
        "city",
        "count(CASE WHEN status = 'paid' THEN 1 END)",
        "sum(a) + sum(b)",
        "cast(sum(x) DECIMAL)",
    ],
)
def test_anything_else_does_not(source):
    assert additivity("m", {"m": source}) == ("none", None, None)


def test_a_name_without_a_source_does_not_add_up():
    assert additivity("nope", SOURCES) == ("none", None, None)


@pytest.mark.parametrize(
    "source",
    [
        "sales_amount / order_count",
        "round(sales_amount / order_count, 2)",
        "try_divide(sales_amount, order_count)",
        "sales_amount / nullif(order_count, 0)",
        "SUM(`line_amount`) / count( order_id )",
        "sum(line_amount) / count(order_id)",
        "cast(`sales_amount` AS DOUBLE) / coalesce(t.order_count, 0)",
    ],
)
def test_a_ratio_of_two_sum_columns(source):
    sources = SOURCES | {"avg_order_value": source}
    assert additivity("avg_order_value", sources) == ("ratio", "sales_amount", "order_count")


@pytest.mark.parametrize(
    "source",
    [
        "sales_amount / customer_count",  # a count DISTINCT does not add up
        "sales_amount / order_count * 100",
        "sum(a) / sum(b)",  # no such columns
        "sales_amount / city",
        "avg_order_value / order_count",  # itself
        "sales_amount / nullif(order_count, 1)",
    ],
)
def test_other_divisions_do_not(source):
    sources = SOURCES | {"avg_order_value": source}
    assert additivity("avg_order_value", sources) == ("none", None, None)


def test_a_side_matches_a_source_as_written():
    # Only the side is unwrapped: sum(line_amount) is not round(sum(line_amount), 2).
    sources = SOURCES | {
        "sales_amount": "round(sum(line_amount), 2)",
        "avg_order_value": "sum(line_amount) / count(order_id)",
    }
    assert additivity("sales_amount", sources) == ("sum", None, None)
    assert additivity("avg_order_value", sources) == ("none", None, None)
    sources["avg_order_value"] = "sales_amount / count(order_id)"
    assert additivity("avg_order_value", sources) == ("ratio", "sales_amount", "order_count")


def test_string_literals_keep_their_exact_text_when_sides_are_compared():
    web = "sum(CASE WHEN channel = 'web shop' THEN amount ELSE 0 END)"
    sources = SOURCES | {"web_amount": web}
    # Case and spaces outside the literal do not count.
    sources["web_share"] = "SUM(case when channel='web shop' then amount else 0 end) / order_count"
    assert additivity("web_share", sources) == ("ratio", "web_amount", "order_count")
    for other in ("'webshop'", "'Web Shop'"):
        sources["web_share"] = web.replace("'web shop'", other) + " / order_count"
        assert additivity("web_share", sources) == ("none", None, None), other


def test_quotes_and_nested_parentheses_do_not_confuse_the_reading():
    sources = {"m": "sum(CASE WHEN note = 'a/b, c)' THEN x END)"}
    assert additivity("m", sources) == ("sum", None, None)
    assert additivity("m", {"m": "count(DISTINCT `a/b`)"}) == ("none", None, None)


def test_defaults_fill_what_the_model_left_out():
    filled = with_defaults(design())
    assert filled.top_n == 10
    assert filled.order_by is not None and filled.order_by[0].model_dump() == {
        "column": "sales_amount",
        "desc": True,
    }
    assert filled.chart is not None
    assert filled.chart.model_dump() == {
        "type": "bar",
        "label": ["city", "member_level"],
        "value": "sales_amount",
    }
    single = with_defaults(design(group_by=None, filter=None))
    assert single.group_by == [] and single.filter == []
    assert single.chart is not None and single.chart.type == "none" and single.chart.label == []
    kept = with_defaults(
        design(
            top_n=3, order_by=[{"column": "city"}], chart={"type": "line", "value": "order_count"}
        )
    )
    assert kept.top_n == 3 and kept.order_by is not None and kept.order_by[0].desc is True
    assert kept.chart is not None and kept.chart.model_dump() == {
        "type": "line",
        "label": ["city", "member_level"],
        "value": "order_count",
    }


def test_a_good_design_has_no_problems():
    assert design_problems(design(), TARGETS) == []
    assert design_problems(with_defaults(design()), TARGETS) == []


@pytest.mark.parametrize(
    "changes, problems",
    [
        (
            {"group_by": ["city", "member_level", "stat_month", "sales_amount"]},
            ["answer.group_by 最多 3 列"],
        ),
        ({"measures": []}, ["answer.measures 要有 1–8 列"]),
        (
            {"measures": ["sales_amount"] * 9},
            ["answer.measures 要有 1–8 列", "answer.measures 的列 sales_amount 重复"],
        ),
        (
            {"group_by": ["city", "city"], "measures": ["order_count", "sales_amount"] * 2},
            [
                "answer.group_by 的列 city 重复",
                "answer.measures 的列 order_count 重复",
                "answer.measures 的列 sales_amount 重复",
            ],
        ),
        (
            {"group_by": ["province"], "measures": ["gmv"]},
            [
                "answer.group_by 的列 province 不在结果表 ads_sales_city_member_m 里",
                "answer.measures 的列 gmv 不在结果表 ads_sales_city_member_m 里",
            ],
        ),
        (
            {"order_by": [{"column": "nope"}, {"column": "stat_month", "desc": False}]},
            [
                "answer.order_by 的列 nope 不在结果表 ads_sales_city_member_m 里",
                "answer.order_by 的列 stat_month 要在 group_by 或 measures 里",
            ],
        ),
        (
            {"filter": [{"column": "nope", "pick": "latest"}]},
            ["answer.filter 的列 nope 不在结果表 ads_sales_city_member_m 里"],
        ),
        (
            {"top_n": 51},
            ["answer.top_n 要在 1–50 之间"],
        ),
        ({"top_n": 0}, ["answer.top_n 要在 1–50 之间"]),
        ({"chart": {"type": "pie"}}, ["answer.chart.type 只能是 bar、line 或 none"]),
        (
            {"chart": {"value": "city", "label": ["stat_month"]}},
            [
                "answer.chart.value city 要在 measures 里",
                "answer.chart.label 的列 stat_month 要在 group_by 里",
            ],
        ),
    ],
)
def test_design_rules(changes, problems):
    assert design_problems(design(**changes), TARGETS) == problems


@pytest.mark.parametrize(
    "value",
    [
        [{"column": "stat_month", "pick": "latest"}, {"column": "city", "pick": "latest"}],
        [{"column": "stat_month", "pick": "first"}],
        [{"column": "stat_month", "pick": "latest", "value": "2026-10"}],
        [{"column": 3, "pick": "latest"}],
        ["stat_month"],
    ],
)
def test_the_first_stage_knows_one_filter_only(value):
    assert design_problems(design(filter=value), TARGETS) == [
        'answer.filter 第一阶段只能写一条 {"column": 列名, "pick": "latest"}'
    ]


def test_a_target_that_is_not_one_is_checked_against_the_last_target():
    problems = design_problems(design(target="nope", group_by=["city"]), TARGETS)
    assert problems == [
        "answer.target nope 不是目标表",
        "answer.measures 的列 sales_amount 不在结果表 order_facts 里",
        "answer.measures 的列 order_count 不在结果表 order_facts 里",
        "answer.measures 的列 customer_count 不在结果表 order_facts 里",
        "answer.measures 的列 avg_order_value 不在结果表 order_facts 里",
        "answer.filter 的列 stat_month 不在结果表 order_facts 里",
    ]


def test_a_generated_design_follows_the_brief_and_the_rules():
    names = list(SOURCES)
    made = generate(
        "ads_sales_city_member_m",
        names,
        ["stat_month", "city", "member_level", "city", "stat_month", "extra"],
        ["sales_amount", "order_count", "sales_amount"],
        "stat_month",
    )
    assert made is not None
    assert made.model_dump() == {
        "target": "ads_sales_city_member_m",
        "filter": [{"column": "stat_month", "pick": "latest"}],
        "group_by": ["city", "member_level", "extra"],
        "measures": ["sales_amount", "order_count"],
        "order_by": [{"column": "sales_amount", "desc": True}],
        "top_n": 10,
        "chart": {
            "type": "bar",
            "label": ["city", "member_level", "extra"],
            "value": "sales_amount",
        },
    }
    many = [f"m{i}" for i in range(10)]
    made = generate("t", ["d", *many], ["d"], many, None)
    assert made is not None and made.filter == [] and made.measures == many[:8]
    assert design_problems(made, {"t": ["d", *many]}) == []
    # A time column the table lacks is not filtered on; no groups: no chart.
    made = generate("t", ["m"], [], ["m"], "stat_month")
    assert made is not None and made.filter == [] and made.chart is not None
    assert made.chart.type == "none"
    assert generate("t", ["d"], ["d"], [], "d") is None


def test_columns_follow_the_contract_example():
    units = {"sales_amount": "元", "order_count": None, "avg_order_value": None}
    assert columns(design(), SOURCES, LABELS, units) == [
        {"name": "city", "label": "城市", "role": "dimension"},
        {"name": "member_level", "label": "会员等级", "role": "dimension"},
        {
            "name": "sales_amount",
            "label": "销售额（元）",
            "role": "metric",
            "agg": "sum",
            "unit": "元",
        },
        {"name": "order_count", "label": "订单数", "role": "metric", "agg": "sum", "unit": None},
        {
            "name": "customer_count",
            "label": "下单客户数",
            "role": "metric",
            "agg": "none",
            "unit": None,
        },
        {
            "name": "avg_order_value",
            "label": "客单价（元）",
            "role": "metric",
            "agg": "ratio",
            "unit": "元",
            "numerator": "sales_amount",
            "denominator": "order_count",
        },
    ]


def test_a_unit_comes_from_the_brief_then_the_label():
    one = design(group_by=[], measures=["sales_amount", "order_count", "customer_count"])
    got = columns(
        one,
        SOURCES,
        {"sales_amount": "销售额(万元) ", "order_count": "订单数（单）"},
        {"order_count": "笔"},
    )
    assert [(c["label"], c["unit"]) for c in got] == [
        ("销售额(万元) ", "万元"),
        ("订单数（单）", "笔"),
        ("customer_count", None),
    ]
    assert all("format" not in c for c in got)


NAMES = {
    "stat_month": "统计月",
    "city": "城市",
    "member_level": "会员等级",
    "sales_amount": "销售额",
}


def test_the_sentence_follows_the_contract_template():
    filled = with_defaults(design())
    assert text(filled, NAMES) == (
        "最近一个统计月里，各「城市 × 会员等级」组合按销售额从高到低排的前 10 组，附合计和柱状图。"
    )
    plain = with_defaults(design(filter=[], top_n=5))
    assert text(plain, NAMES) == (
        "各「城市 × 会员等级」组合按销售额从高到低排的前 5 组，附合计和柱状图。"
    )
    low = with_defaults(design(order_by=[{"column": "order_count", "desc": False}]))
    assert text(low, NAMES) == (
        "最近一个统计月里，各「城市 × 会员等级」组合按order_count从低到高排的前 10 组，"
        "附合计和柱状图。"
    )
    line = with_defaults(design(group_by=["city"], chart={"type": "line"}))
    assert text(line, NAMES).endswith("前 10 组，附合计和折线图。")
    bare = with_defaults(design(group_by=["city"], chart={"type": "none"}))
    assert text(bare, NAMES).endswith("前 10 组，附合计。")


def test_without_groups_the_sentence_names_the_totals():
    total = with_defaults(design(group_by=[], measures=["sales_amount", "order_count"]))
    names = NAMES | {"order_count": "订单数"}
    assert text(total, names) == "最近一个统计月的销售额、订单数合计。"
    total = with_defaults(design(group_by=[], filter=[], measures=["sales_amount"]))
    assert text(total, names) == "销售额合计。"
