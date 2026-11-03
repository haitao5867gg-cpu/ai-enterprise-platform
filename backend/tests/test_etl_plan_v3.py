"""方案第 3 版 (docs/api/data-nodes.md, 第三批): ways of writing a table, the project's
prefix and run parameters, the data checks a plan proposes, and what the plan check node
gives the data check node."""

import copy
from typing import Any

import pytest

from aep.etl.plan import (
    OUTPUT_FORMAT,
    OUTPUT_FORMAT_V2,
    OUTPUT_FORMAT_V3,
    EtlPlan,
    EtlPlanV3,
    PlanError,
    validate_plan_v2,
    validate_plan_v3,
)
from aep.runtime.flow.nodes import NodeFailure
from tests.test_etl_plan_v2 import HIT, KNOWN, SEEN, SOURCES, brief_plan
from tests.test_flow_data_nodes import (
    brief_reply,
    check_node,
    node_env,
    profiled,
    run,
    v2_state,
)

PROJECT: dict[str, Any] = {
    "project_id": "retail",
    "name": "零售",
    "table_prefix": "",
    "references": [{"name": "prev.sales.Forecast", "filter": "Type = 'Vol.'"}],
    "params": [{"key": "update_month", "label": "统计月", "type": "month"}],
    "values": {"update_month": "2026-10"},
}
TOTAL = {
    "id": "total",
    "label": "订单数不为负",
    "kind": "aggregate",
    "table": "city_daily",
    "expr": "sum(order_count)",
    "op": "ge",
    "value": 0,
}
KEYS = {
    "id": "keys",
    "label": "每天每个城市一行",
    "kind": "unique",
    "table": "city_daily",
    "columns": ["stat_day", "city"],
    "severity": "warn",
}
RECONCILE = {
    "id": "orders",
    "label": "明细和订单表对得上",
    "kind": "reconcile",
    "left": {"table": "order_facts", "expr": "count(*)"},
    "right": {"table": "sales.orders", "expr": "count(*)", "where": "status = 'paid'"},
}


def v3_plan(*checks: dict, **changes: Any) -> dict:
    """``brief_plan`` written the version 3 way: the result replaces one month."""
    raw = copy.deepcopy(brief_plan(**changes))
    raw["targets"][1]["load"] = {
        "mode": "replace_where",
        "where": "stat_day like concat(${param.update_month}, '%')",
    }
    raw["checks"] = list(checks)
    return raw


def check3(raw: dict, project: dict | None = PROJECT) -> tuple[EtlPlanV3, list[str]]:
    return validate_plan_v3(
        raw, SOURCES, KNOWN, SEEN, references=[HIT], project=copy.deepcopy(project)
    )


def problems3(raw: dict, project: dict | None = PROJECT) -> list[str]:
    with pytest.raises(PlanError) as failed:
        check3(raw, project)
    return failed.value.problems


def test_a_good_version_three_plan():
    plan, cleaned = check3(v3_plan(TOTAL, KEYS, RECONCILE))
    assert cleaned == []
    # The detail table has no load: it is overwritten.
    assert [t.load.mode for t in plan.targets] == ["overwrite", "replace_where"]
    assert plan.targets[1].load.stored(plan.targets[1].primary_key) == {
        "mode": "replace_where",
        "where": "stat_day like concat(${param.update_month}, '%')",
        "keys": ["stat_day", "city"],
    }
    # Checks keep what they say; a missing severity is warn (a model's check only warns
    # unless the plan says it must hold).
    assert plan.checks == [{**TOTAL, "severity": "warn"}, KEYS, {**RECONCILE, "severity": "warn"}]
    dumped = plan.model_dump()
    assert dumped["checks"] == plan.checks
    # The code check and the dev run read it as a version 1 plan.
    assert EtlPlan.model_validate(dumped).targets[1].load.mode == "replace_where"

    # No checks is fine; so is a run without a project when no parameter is used.
    raw = v3_plan()
    raw["targets"][1]["load"] = {"mode": "merge", "keys": ["stat_day", "city"]}
    del raw["checks"]
    plan, _ = check3(raw, None)
    assert plan.checks == [] and plan.targets[1].load.keys == ["stat_day", "city"]


def test_version_three_load_modes_are_checked():
    def load_problems(load: dict, project: dict | None = PROJECT) -> list[str]:
        raw = v3_plan()
        raw["targets"][1]["load"] = load
        return problems3(raw, project)

    assert load_problems({"mode": "replace_where"}) == [
        "city_daily 按条件替换（replace_where），要给出替换条件 load.where"
    ]
    assert load_problems({"mode": "replace_where", "where": "a = 1; drop table x"}) == [
        "city_daily 的 load.where：替换条件不能有分号"
    ]
    assert load_problems({"mode": "replace_where", "where": "m = ${month}"}) == [
        "city_daily 的 load.where：运行参数要写成 ${param.<key>}（key 是项目参数的标识）"
    ]
    assert load_problems({"mode": "replace_where", "where": "m = ${param.month}"}) == [
        "city_daily 的 load.where 的运行参数 ${param.month} 不是项目「零售」定义的参数"
    ]
    assert load_problems({"mode": "replace_where", "where": "m = ${param.update_month}"}, None) == [
        "city_daily 的 load.where 用了运行参数 ${param.update_month}，"
        "这次运行没有绑定项目，不能用运行参数"
    ]
    assert load_problems({"mode": "overwrite", "where": "city = '上海'", "keys": ["city"]}) == [
        "city_daily 的 load.where 只用在按条件替换（replace_where）",
        "city_daily 的 load.keys 只用在按键合并（merge）",
    ]
    assert load_problems({"mode": "merge", "keys": ["city", "town", "city"]}) == [
        "city_daily 的合并键 'city' 重复",
        "city_daily 的合并键 'town' 不在目标列里",
    ]
    assert load_problems({"mode": "append", "key": "stat_day"}) == [
        "city_daily 的 load.key 在这一版不用：按条件替换写 load.where，按键合并写 load.keys"
    ]
    # Versions 1 and 2's modes, and modes that do not exist.
    assert load_problems({"mode": "full"}) == [
        "city_daily 的 load.mode 只能是 overwrite、replace_where、append、merge"
    ]
    assert load_problems({"mode": "upsert"}) == [
        "targets[1].load.mode 只能是 overwrite、replace_where、append、merge"
    ]
    # Merging needs keys: its own or the primary key.
    raw = v3_plan()
    raw["targets"][1] |= {"load": {"mode": "merge"}, "primary_key": []}
    assert problems3(raw) == [
        "city_daily 按键合并（merge），要给出合并键 load.keys 或主键 primary_key"
    ]


def test_version_two_keeps_its_modes():
    raw = brief_plan()
    raw["targets"][1]["load"] = {"mode": "overwrite"}
    with pytest.raises(PlanError) as failed:
        validate_plan_v2(raw, SOURCES, KNOWN, SEEN)
    assert failed.value.problems == ["city_daily 的 load.mode 只能是 full、incremental"]
    # Version 2 ignores checks.
    plan, _ = validate_plan_v2(brief_plan(checks=[{"nonsense": 1}]), SOURCES, KNOWN, SEEN)
    assert not hasattr(plan, "checks")


def test_the_projects_prefix_and_reference_names():
    project = PROJECT | {"table_prefix": "rt_"}
    assert problems3(v3_plan(), project) == [
        "目标表名 'order_facts' 要以项目「零售」的表名前缀 rt_ 开头",
        "目标表名 'city_daily' 要以项目「零售」的表名前缀 rt_ 开头",
    ]
    raw = v3_plan()
    raw["targets"][1]["name"] = raw["answer"]["target"] = "forecast"
    assert problems3(raw) == [
        "目标表名 'forecast' 和项目引用的表 prev.sales.Forecast 同名，请换一个"
    ]
    # Without a project neither is checked.
    raw["targets"][1]["load"] = {"mode": "overwrite"}
    assert check3(raw, None)[0].targets[1].name == "forecast"


def test_proposed_checks_are_checked():
    golden = {
        "id": "old",
        "label": "和老系统一致",
        "kind": "golden",
        "table": "city_daily",
        "expected": "old.jade.city_daily",
        "keys": ["stat_day", "city"],
    }
    found = problems3(
        v3_plan(
            TOTAL,
            {**KEYS, "id": "total"},
            {**TOTAL, "id": "golden"},
            golden,
            {**TOTAL, "id": "elsewhere", "table": "sales.refunds"},
            {**RECONCILE, "right": {**RECONCILE["right"], "table": "prev.sales.forecast"}},
            {**TOTAL, "id": "bad", "op": "between"},
            {"id": "rows", "label": "x", "kind": "rows", "table": "city_daily", "sql": "1"},
            "?",
        )
    )
    assert found == [
        "checks[1].id：检查 id total 重复",
        "checks[2].id：id golden 留给数据检查节点和老系统的对比，请换一个",
        "checks[3].kind：方案里的检查只能是 aggregate、reconcile、unique、rows；"
        "和老系统的逐行对比由数据检查节点加",
        "checks[4].table：sales.refunds 不是方案的目标表或来源，检查只能读方案写的表和读的源表",
        "checks[5].right.table：prev.sales.forecast 不是方案的目标表或来源，"
        "检查只能读方案写的表和读的源表",
        "checks[6].value：between 要写数字 value 和 max",
        "checks[7].sql：rows 检查没有 sql 这一项",
        "checks[7].where：条件不能为空",
        "checks[8]：每条检查要写成一个对象",
    ]
    assert problems3(v3_plan(*[{**TOTAL, "id": f"c{i}"} for i in range(11)])) == [
        "checks 最多 10 条，现在有 11 条"
    ]
    assert problems3(v3_plan() | {"checks": {"total": TOTAL}}) == ["checks 应该是列表"]
    # Run parameters: a project's, and none without one.
    month = {**TOTAL, "where": "stat_day like ${param.update_month}"}
    assert check3(v3_plan(month))[0].checks[0]["where"] == month["where"]
    wrong = {**TOTAL, "where": "stat_day like ${param.month}"}
    assert problems3(v3_plan(wrong)) == [
        "checks[0].where 的运行参数 ${param.month} 不是项目「零售」定义的参数"
    ]
    raw = v3_plan(month)
    raw["targets"][1]["load"] = {"mode": "overwrite"}
    assert problems3(raw, None) == [
        "checks[0].where 用了运行参数 ${param.update_month}，这次运行没有绑定项目，不能用运行参数"
    ]


def test_a_proposed_check_names_columns_its_tables_have():
    # jadeflow retest I-10: a model check said max(c) and the result table has no c.
    def found(*proposed: dict, sources: dict | None = None) -> list[str]:
        with pytest.raises(PlanError) as failed:
            validate_plan_v3(
                v3_plan(*proposed),
                SOURCES,
                KNOWN,
                SEEN,
                references=[HIT],
                project=copy.deepcopy(PROJECT),
                source_columns=sources,
            )
        return failed.value.problems

    plan, _ = check3(v3_plan({**TOTAL, "where": "city IS NOT NULL AND `stat_day` > '2026'"}))
    assert len(plan.checks) == 1
    have = "avg_quantity、city、order_count、quantity_sum、stat_day"
    assert found({**TOTAL, "expr": "max(c)", "where": "Order_Count > 0 AND city <> 'x'"}) == [
        f"checks[0].expr：city_daily 没有列 c，检查里只能用表里有的列：{have}"
    ]
    assert found({**KEYS, "columns": ["stat_day", "town"]}) == [
        f"checks[0].columns：city_daily 没有列 town，检查里只能用表里有的列：{have}"
    ]
    # A source's columns are known from the profile; a table not profiled is not looked at.
    orders = {"sales.orders": ["order_id", "status"]}
    wrong = {**RECONCILE, "right": {**RECONCILE["right"], "where": "state = 'paid'"}}
    assert found(wrong, sources=orders) == [
        "checks[0].right.where：sales.orders 没有列 state，检查里只能用表里有的列：order_id、status"
    ]
    assert found(wrong, {**TOTAL, "expr": "max(c)"}) == [
        f"checks[1].expr：city_daily 没有列 c，检查里只能用表里有的列：{have}"
    ]


def test_output_formats():
    # Versions 1 and 2 stay as they are; version 3 asks for write modes and checks.
    for old in (OUTPUT_FORMAT, OUTPUT_FORMAT_V2):
        assert "replace_where" not in old and '"checks"' not in old
    for text in ("overwrite、replace_where、append 或 merge", '"checks"', "${param."):
        assert text in OUTPUT_FORMAT_V3


# The plan check node, format 3


async def test_the_node_gives_the_data_check_its_table_keys_and_checks():
    rt, ds = await node_env()
    tables = await profiled(rt, ds)
    raw = brief_reply()
    raw["targets"][0]["load"] = {"mode": "overwrite"}
    raw["targets"][1]["load"] = {"mode": "merge"}
    raw["checks"] = [
        {**KEYS, "table": "city_totals", "columns": ["city"]},
        {**RECONCILE, "left": {"table": "order_facts", "expr": "sum(quantity)"},
         "right": {"table": "sales.orders", "expr": "sum(quantity)"}},
    ]  # fmt: skip
    st = v2_state(tables, raw) | {"project": PROJECT}
    out = await run(check_node(format=3, answered="{{review.record}}"), st, rt)
    assert out.branch == "ok", out.outputs["problems"]
    o = out.outputs
    assert (o["result_table"], o["result_keys"]) == ("city_totals", ["city"])
    assert o["checks"] == o["plan"]["checks"] and [c["id"] for c in o["checks"]] == [
        "keys",
        "orders",
    ]
    message = o["message"]
    for text in (
        "- 写表方式：整表覆盖",
        "- 写表方式：按 `city` 合并，有的更新，没有的插入",
        "### 数据检查",
        "- **每天每个城市一行**（警告）：键不重复，`city_totals` 按 `city` 没有重复的行",
        "- **明细和订单表对得上**（警告）：对账，`order_facts` 的 `sum(quantity)`"
        "和 `sales.orders` 的 `sum(quantity)`一致",
    ):
        assert text in message, message
    assert message.index("### 数据检查") > message.index("### 依据的资料")
    # What a version 2 plan says, version 3 says too.
    assert o["questions"][0]["key"] == "q1" and o["brief"]["goal"].startswith("按城市统计")

    # A problem goes back to the model, with the new outputs empty.
    raw["checks"][0]["table"] = "sales.refunds"
    failed = await run(check_node(format=3), v2_state(tables, raw) | {"project": PROJECT}, rt)
    assert failed.branch == "retry"
    assert "checks[0].table：sales.refunds 不是方案的目标表或来源" in failed.outputs["problems"]
    assert (failed.outputs["result_table"], failed.outputs["result_keys"]) == ("", [])
    assert failed.outputs["checks"] == []

    # Unbound: no prefix checked, no parameters.
    raw["checks"] = [{**TOTAL, "table": "city_totals", "where": "x = ${param.update_month}"}]
    raw["checks"][0]["expr"] = "sum(order_count)"
    unbound = await run(check_node(format=3), v2_state(tables, raw), rt)
    assert unbound.branch == "retry"
    assert "这次运行没有绑定项目，不能用运行参数" in unbound.outputs["problems"]


async def test_versions_one_and_two_leave_the_new_outputs_empty():
    rt, ds = await node_env()
    tables = await profiled(rt, ds)
    for node, raw in ((check_node(), brief_reply()), (check_node(format=2), brief_reply())):
        o = (await run(node, v2_state(tables, raw), rt)).outputs
        assert o["ok"] is True
        assert (o["result_table"], o["result_keys"], o["checks"]) == ("", [], [])
        assert "### 数据检查" not in o["message"]


def test_the_projects_acceptance_checks_and_filtered_sources():
    # A plan check may not take the id of one of the project's acceptance checks.
    owned = {**PROJECT, "checks": [{**TOTAL, "id": "keys"}]}
    assert problems3(v3_plan(TOTAL, KEYS), owned) == [
        "checks[1].id：keys 是项目的验收检查，数据检查时会自动跑，方案里不用再写；"
        "要另加检查请换一个 id"
    ]
    # SQL code cannot read a reference with a read filter: the platform writes the plan
    # in PySpark instead of sending it back, and says so.
    filtered = {**PROJECT, "references": [{"name": "SALES.orders", "filter": "status <> 'test'"}]}
    raw = v3_plan()
    raw["language"] = "sql"
    plan, cleaned = check3(raw, filtered)
    assert plan.language == "pyspark"
    assert "sales.orders 有项目的读取筛选，SQL 代码暂不支持，代码语言改成了 PySpark" in cleaned
    raw["language"] = "pyspark"
    assert check3(raw, filtered)[1] == []


def test_empty_parts_the_model_writes_out_do_not_send_the_plan_back():
    # The format says null or [] for parts that do not apply; either is fine, and so is a
    # check that writes every field of every kind with null where it does not apply.
    raw = v3_plan(
        {**TOTAL, "group_by": [], "where": "", "max": None, "severity": None, "left": None},
        {**RECONCILE, "group_by": [], "right": {**RECONCILE["right"], "where": None}},
        {**KEYS, "where": None, "expr": None},
    )
    raw["targets"][0]["load"] = {"mode": "overwrite", "where": None, "keys": None, "key": None}
    raw["targets"][1]["load"]["keys"] = None
    plan, cleaned = check3(raw)
    assert cleaned == []
    assert [c["id"] for c in plan.checks] == ["total", "orders", "keys"]
    assert plan.checks[0]["severity"] == "warn"
    assert "group_by" not in plan.checks[0] and "where" not in plan.checks[0]
    assert plan.targets[1].load.keys == []
    # What still matters still goes back: replacing rows needs its condition.
    raw["targets"][1]["load"]["where"] = " "
    assert problems3(raw) == ["city_daily 按条件替换（replace_where），要给出替换条件 load.where"]


def test_quality_rule_names_are_made_usable_by_the_platform():
    raw = v3_plan()
    rules = [
        {"name": "数量为正", "condition": "quantity > 0", "on_violation": "drop"},
        {"name": "City-Not-Blank", "condition": "city <> ''", "on_violation": "warn"},
        {"name": "rule_1", "condition": "order_id IS NOT NULL", "on_violation": "fail"},
        {"name": "rule_1", "condition": "quantity < 1000", "on_violation": "warn"},
    ]
    raw["targets"][0]["quality_rules"] = rules
    plan, cleaned = check3(raw)
    assert [r.name for r in plan.targets[0].quality_rules] == [
        "rule_1_2",
        "city_not_blank",
        "rule_1",
        "rule_1_3",
    ]
    assert cleaned == [
        "order_facts 的质量规则名改成了代码能用的名字："
        "「数量为正」→ rule_1_2、「City-Not-Blank」→ city_not_blank、「rule_1」→ rule_1_3"
    ]


# 表结构变化: a target named like one of the project's tables in the environment writes to
# it. A write that keeps its other rows keeps its columns; the columns the plan adds go
# into load.add_columns, where the plan puts them.

CITY_DAILY = {
    "name": "City_Daily",  # not case sensitive
    "table": "c.s.City_Daily",
    "columns": [
        {"name": n, "data_type": "string", "nullable": True, "comment": None}
        for n in ("stat_day", "city", "order_count")
    ],
}


def check_existing(raw: dict, existing: list[dict] | None) -> tuple[EtlPlanV3, list[str]]:
    return validate_plan_v3(
        raw,
        SOURCES,
        KNOWN,
        SEEN,
        references=[HIT],
        project=copy.deepcopy(PROJECT),
        existing=existing,
    )


# The types of the columns v3_plan adds to CITY_DAILY, as the model writes them.
ADDED_TYPES = {"quantity_sum": "BIGINT", "avg_quantity": "decimal(10, 2)"}


def typed(raw: dict, types: dict[str, str | None] = ADDED_TYPES, target: int = 1) -> dict:
    for column in raw["targets"][target]["columns"]:
        if column["name"] in types:
            column["type"] = types[column["name"]]
    return raw


def test_columns_added_to_an_existing_table_are_worked_out_by_the_platform():
    raw = typed(v3_plan())
    # What the model writes there is the platform's to set.
    raw["targets"][1]["load"]["add_columns"] = [{"name": "nonsense", "after": None}]
    plan, _ = check_existing(raw, [CITY_DAILY])
    load = plan.targets[1].load
    added = [
        {"name": "quantity_sum", "after": "order_count", "type": "bigint"},
        {"name": "avg_quantity", "after": "quantity_sum", "type": "decimal(10,2)"},
    ]
    assert [c.model_dump() for c in load.add_columns] == added
    assert load.stored(plan.targets[1].primary_key)["add_columns"] == added
    # Types are written the way Spark writes them; a column without one dumps as before.
    columns = plan.targets[1].columns
    assert [c.type for c in columns] == [None, None, None, "bigint", "decimal(10,2)"]
    assert "type" not in columns[0].model_dump()
    # order_facts is not in the environment yet: nothing to add, nothing stored.
    assert plan.targets[0].load.add_columns == []
    assert "add_columns" not in plan.targets[0].load.stored(["order_id"])

    # A new first column goes first.
    raw = typed(v3_plan())
    columns = raw["targets"][1]["columns"]
    columns.insert(0, columns.pop(3))  # quantity_sum
    plan, _ = check_existing(raw, [CITY_DAILY])
    assert [c.model_dump() for c in plan.targets[1].load.add_columns] == [
        {"name": "quantity_sum", "after": None, "type": "bigint"},
        {"name": "avg_quantity", "after": "order_count", "type": "decimal(10,2)"},
    ]

    # Overwriting rebuilds the table; existing tables unknown: nothing is added either,
    # and no type is needed.
    raw = v3_plan()
    raw["targets"][1]["load"] = {"mode": "overwrite"}
    assert check_existing(raw, [CITY_DAILY])[0].targets[1].load.add_columns == []
    assert check_existing(v3_plan(), None)[0].targets[1].load.add_columns == []


def test_a_column_added_to_an_existing_table_has_a_type_people_confirm():
    def problems(raw: dict) -> list[str]:
        with pytest.raises(PlanError) as failed:
            check_existing(raw, [CITY_DAILY])
        return failed.value.problems

    assert problems(typed(v3_plan(), {"quantity_sum": None, "avg_quantity": " "})) == [
        f"city_daily 已经存在，要新加列 {name}：请在 columns 里写它的 type（Spark SQL 类型，"
        "如 STRING、BIGINT、DECIMAL(18,2)、DATE），确认方案时人要看到加的是什么类型的列"
        for name in ("quantity_sum", "avg_quantity")
    ]
    assert problems(typed(v3_plan(), ADDED_TYPES | {"quantity_sum": "number"})) == [
        "city_daily 的列 quantity_sum 的 type number 不是 Spark SQL 类型（如 STRING、INT、"
        "BIGINT、DOUBLE、DECIMAL(18,2)、DATE、TIMESTAMP、ARRAY<STRING>）"
    ]
    # A column the table has keeps its type: the plan may say it, never change it.
    assert problems(typed(v3_plan(), ADDED_TYPES | {"order_count": "BIGINT"})) == [
        "city_daily 的列 order_count 在表里是 STRING，方案写的是 BIGINT："
        "已有的列不改类型，type 照表里的写或者不写"
    ]
    check_existing(typed(v3_plan(), ADDED_TYPES | {"order_count": "VARCHAR(20)"}), [CITY_DAILY])


def test_a_write_that_keeps_the_rows_keeps_every_column():
    wide = copy.deepcopy(CITY_DAILY)
    wide["columns"].insert(2, {"name": "Region", "data_type": "string"})
    wide["columns"].append({"name": "loaded_at", "data_type": "timestamp"})
    for mode in ({"mode": "append"}, {"mode": "merge"}):
        raw = typed(v3_plan())
        raw["targets"][1]["load"] = mode
        with pytest.raises(PlanError) as failed:
            check_existing(raw, [wide])
        name = {"append": "追加", "merge": "按键合并"}[mode["mode"]]
        assert failed.value.problems == [
            "city_daily 已经存在，表里的列 Region、loaded_at 不在方案的 columns 里："
            f"{name}会保留表里其他的行，要写出表里已有的全部列，新加的列写在想放的位置；"
            "要去掉列或重建表，请用 overwrite"
        ]
    raw = v3_plan()
    raw["targets"][1]["load"] = {"mode": "overwrite"}
    check_existing(raw, [wide])  # rebuilt as the plan says


async def test_the_node_says_what_writing_does_to_each_table():
    rt, ds = await node_env()
    tables = await profiled(rt, ds)
    totals = {
        "name": "city_totals",
        "table": "c.s.city_totals",
        "columns": [{"name": "city", "data_type": "string"}, {"name": "order_count"}],
    }

    def with_existing(raw: dict, existing: Any) -> dict:
        st = v2_state(tables, raw) | {"project": PROJECT}
        st["vars"]["profile"]["existing"] = existing
        return st

    node = check_node(format=3, existing="{{profile.existing}}")
    raw = typed(brief_reply(), {"quantity_sum": "bigint", "avg_quantity": "double"})
    raw["targets"][0]["load"] = {"mode": "overwrite"}
    raw["targets"][1]["load"] = {"mode": "merge"}
    out = await run(node, with_existing(raw, [totals]), rt)
    assert out.branch == "ok", out.outputs["problems"]
    assert out.outputs["plan"]["targets"][1]["load"]["add_columns"] == [
        {"name": "quantity_sum", "after": "order_count", "type": "bigint"},
        {"name": "avg_quantity", "after": "quantity_sum", "type": "double"},
    ]
    message = out.outputs["message"]
    assert "- 表结构：表还不存在，按方案新建" in message  # order_facts
    assert (
        "- 表结构：表已经存在（2 列），确认方案后加 2 列：`quantity_sum` BIGINT，可为空（放在 "
        "`order_count` 后面）；`avg_quantity` DOUBLE，可为空（放在 `quantity_sum` 后面）"
    ) in message

    # The same columns: nothing changes. Overwriting says it drops the rows.
    same = totals | {"columns": [{"name": c["name"]} for c in raw["targets"][1]["columns"]]}
    out = await run(node, with_existing(raw, [same]), rt)
    assert "- 表结构：表已经存在（4 列），结构不变" in out.outputs["message"]
    raw["targets"][1]["load"] = {"mode": "overwrite"}
    out = await run(node, with_existing(raw, [totals]), rt)
    assert "整表覆盖会删掉现有的全部行，按方案重建表结构" in out.outputs["message"]
    assert out.outputs["plan"]["targets"][1]["load"]["add_columns"] == []

    # Not listed (None): no word on the structure.
    out = await run(node, with_existing(raw, None), rt)
    assert out.branch == "ok" and "表结构" not in out.outputs["message"]
    # Something else there is the app's mistake.
    with pytest.raises(NodeFailure, match="已有的表要引用探查数据节点的 existing"):
        await run(node, with_existing(raw, "city_totals"), rt)


async def test_what_a_plan_lost_after_it_was_sent_back_is_said():
    rt, ds = await node_env()
    tables = await profiled(rt, ds)
    node = check_node(format=3)
    project = PROJECT | {"checks": [{"id": "c8_fixed", "kind": "rows"}]}
    asked = {
        "id": "k1",
        "label": "子查询检查",
        "kind": "rows",
        "table": "city_totals",
        "where": "city NOT IN (SELECT city FROM order_facts)",
    }

    def written(**changes: Any) -> dict:
        raw = brief_reply(**changes)
        for target in raw["targets"]:
            target["load"] = {"mode": "overwrite"}
        return raw

    def attempt(raw: dict, before: Any = None) -> dict:
        st = v2_state(tables, raw) | {"project": project}
        st["branch"] = {"check_plan": "retry"} if before is not None else {}
        st["private"] = {"check_plan": before} if before is not None else {}
        return st

    # Sent back: a subquery the platform does not take, a project check's id.
    first = await run(node, attempt(written(checks=[asked, {"id": "c8_fixed"}])), rt)
    assert first.branch == "retry"
    held = first.private["rejected"]
    (item,) = [i for i in held if "check" in i and i["check"] == ["k1", "子查询检查"]]
    assert item["text"] == "检查「子查询检查」"
    said = "table city_totals，where city NOT IN (SELECT city FROM order_facts)"
    assert item["said"] == said

    # The model drops the check and a column instead of saying so: the person is told.
    passing = written(checks=[])
    passing["brief"]["metrics"].pop()  # avg_quantity
    passing["targets"][1]["columns"].pop()
    out = await run(node, attempt(passing, first.private), rt)
    assert out.branch == "ok", out.outputs["problems"]
    assert out.outputs["dropped"] == [
        f"去掉了检查「子查询检查」（{said}）",
        "去掉了 city_totals 的列 avg_quantity",
    ]
    assert out.outputs["message"].startswith(
        "> 注意：方案被平台校验退回后，这一版和退回的那版不一样：去掉了检查「子查询检查」"
        f"（{said}）；去掉了 city_totals 的列 avg_quantity。请对照需求确认还要不要"
    )
    assert "rejected" not in out.private

    # jadeflow retest I-15: the same check under another id and label is no loss.
    good = asked | {"where": "city IS NULL"}
    second = await run(node, attempt(written(checks=[good, {"id": "c8_fixed"}])), rt)
    renamed = written(checks=[good | {"id": "k2", "label": "城市不为空"}])
    out = await run(node, attempt(renamed, second.private), rt)
    assert out.branch == "ok" and out.outputs["dropped"] == []
    assert not out.outputs["message"].startswith("> 注意")
    # Kept by its label but checking something else: before and after.
    out = await run(node, attempt(written(checks=[asked | {"id": "k2", "where": "city IS NULL"}]),
                                  first.private), rt)  # fmt: skip
    assert out.outputs["dropped"] == [
        f"检查「子查询检查」改了写法：原来是“{said}”，现在是“city_totals 里没有满足 "
        "city IS NULL 的行”，请确认是不是一个意思"
    ]
    # Dropped, and another check added: is it a replacement?
    other = {"id": "k3", "label": "城市在订单里", "kind": "rows", "table": "city_totals",
             "where": "city = ''"}  # fmt: skip
    out = await run(node, attempt(written(checks=[other]), first.private), rt)
    assert out.outputs["dropped"] == [
        f"去掉了检查「子查询检查」（{said}）",
        "新加了检查「城市在订单里」（city_totals 里没有满足 city = '' 的行）："
        "请确认它是不是替代去掉的那条、够不够",
    ]
    out = await run(node, attempt(written(checks=[])), rt)
    assert out.outputs["dropped"] == []
