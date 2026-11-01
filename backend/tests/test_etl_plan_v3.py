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
    # Checks keep what they say; a missing severity is error.
    assert plan.checks == [{**TOTAL, "severity": "error"}, KEYS, {**RECONCILE, "severity": "error"}]
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
        "- **明细和订单表对得上**：对账，`order_facts` 的 `sum(quantity)`"
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
    # SQL code cannot read a reference with a read filter: the plan says so already.
    filtered = {**PROJECT, "references": [{"name": "SALES.orders", "filter": "status <> 'test'"}]}
    raw = v3_plan()
    raw["language"] = "sql"
    assert problems3(raw, filtered) == [
        "order_facts 的来源 sales.orders 有项目的读取筛选，SQL 代码暂不支持；"
        "请把 language 改成 pyspark"
    ]
    raw["language"] = "pyspark"
    assert check3(raw, filtered)[1] == []
