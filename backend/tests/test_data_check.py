"""The node data_check (docs/api/data-nodes.md, "data_check 数据检查") on the fake
Databricks connector: which tools each kind of check calls and with what, how results
are judged, the node's golden comparison, only_if, and its settings."""

from typing import Any

import pytest

from aep.config import Settings
from aep.datasources import MemoryDataSourceStore
from aep.etl import checks
from aep.runtime.flow.nodes import TYPES, CheckEnv, NodeFailure, Problems, Runtime
from aep.skills import MemorySkillStore
from aep.tools.mcp import McpTools
from tests import fake_connector
from tests.conftest import SETTINGS, FakeGateway

NOT_DATABRICKS = "数据检查要用 Databricks 数据源"
ID_RULE = "检查 id 只能用小写字母、数字和下划线，以字母开头，最多 32 个字符"


def _col(name: str, data_type: str) -> dict:
    return {
        "name": name,
        "data_type": data_type,
        "nullable": True,
        "primary_key": False,
        "comment": None,
    }


RESULT = [
    {"sku": "A1", "month": "2026-10", "qty": 10, "amount": 100.0},
    {"sku": "B2", "month": "2026-11", "qty": 4, "amount": 40.0},
    {"sku": "C3", "month": "2026-11", "qty": -1, "amount": 0.5},
]
OLD = [
    {"sku": "A1", "month": "2026-10", "qty": 10, "amount": 100.00004},
    {"sku": "B2", "month": "2026-11", "qty": 5, "amount": 40.0},
    {"sku": "D4", "month": "2026-11", "qty": 1, "amount": 1.0},
]
COLUMNS = [_col("sku", "string"), _col("month", "string"), _col("qty", "int"),
           _col("amount", "double")]  # fmt: skip
PROJECT = {
    "project_id": "ibf",
    "name": "IBF 预测",
    "table_prefix": "ibf_",
    "environment": {"key": "dev", "label": "开发", "catalog": "dev", "schema": "etl"},
    "references": [
        {"name": "PREV.sales.Forecast", "label": "预测表", "filter": "Type = 'Vol.'"},
        {"name": "prev.sales.sku", "label": "物料", "filter": None},
    ],
    "adopted": [],
    "params": [{"key": "update_month", "label": "统计月", "type": "month"}],
    "values": {"update_month": "2026-10"},
}


@pytest.fixture(autouse=True)
def tables(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, rows in [(("dev.etl", "ibf_table2"), RESULT), (("old.jade", "table2"), OLD)]:
        monkeypatch.setitem(
            fake_connector.DATABRICKS_TABLES,
            key,
            {"comment": None, "columns": COLUMNS, "foreign_keys": [], "rows": rows},
        )


async def env(
    settings: Settings = SETTINGS,
) -> tuple[Runtime, str, str, list[tuple[str, dict]]]:
    sources = MemoryDataSourceStore()
    lake = await sources.create("数据湖", "databricks", fake_connector.DATABRICKS_URL)
    shop = await sources.create("示例库", "postgres", fake_connector.URL)
    calls: list[tuple[str, dict]] = []
    servers = {
        fake_connector.DATABRICKS_URL: fake_connector.build_databricks(calls),
        fake_connector.URL: fake_connector.build(),
    }
    tools = McpTools(5, resolve=lambda url: servers[url])
    rt = Runtime(
        FakeGateway().client, "mock", {}, {}, datasources=sources, tools=tools, settings=settings
    )
    return rt, lake.datasource_id, shop.datasource_id, calls


def node(**changes: Any) -> dict:
    return {
        "id": "checks",
        "type": "data_check",
        "title": "数据检查",
        "datasource": "{{start.datasource}}",
    } | changes


def state(lake: str, project: dict | None = PROJECT, **variables: Any) -> dict:
    found: dict[str, Any] = {
        "input": {},
        "vars": {"start": {"datasource": lake}} | variables,
        "run_id": "run-1",
    }
    if project is not None:
        found["project"] = project
    return found


async def run(n: dict, st: dict, rt: Runtime):
    return await TYPES["data_check"].run(n, st, rt)


def check(cid: str, kind: str, **fields: Any) -> dict:
    return {"id": cid, "label": f"检查 {cid}", "kind": kind} | fields


async def test_aggregate_reads_the_result_table_in_the_project_environment():
    rt, lake, _, calls = await env()
    n = node(
        checks=[
            check("total", "aggregate", table="ibf_table2", expr="sum(qty)", op="eq", value=13),
            check("count", "aggregate", table="ibf_table2", expr="count(*)", op="ge", value=5),
            check(
                "range",
                "aggregate",
                table="ibf_table2",
                expr="max(qty)",
                op="between",
                value=0,
                max=9.5,
                tolerance=0.5,
                where="month = ${param.update_month}",
            ),
        ]
    )
    out = await run(n, state(lake), rt)
    total, count, ranged = out.outputs["results"]
    assert total | {"rows": None} == {
        "id": "total",
        "label": "检查 total",
        "kind": "aggregate",
        "severity": "error",
        "status": "passed",
        "actual": 13,
        "expected": 13,
        "message": "实际 13，要求 = 13，满足",
        "rows": None,
    }
    assert (count["status"], count["message"]) == ("failed", "实际 3，要求 ≥ 5，不满足")
    # Within the tolerance; the run parameter became a quoted literal.
    assert (ranged["status"], ranged["expected"]) == ("passed", [0, 9.5])
    assert ranged["message"] == "实际 10，要求 在 0 和 9.5 之间（容差 0.5），满足"
    assert calls[0] == ("aggregate", {"table": "dev.etl.ibf_table2", "expr": "sum(qty)",
                                      "where": None, "group_by": None, "limit": 20})  # fmt: skip
    assert calls[2][1]["where"] == "month = '2026-10'"
    assert out.outputs["ok"] is False and out.branch == "failed"
    assert out.outputs["summary"] == "3 项检查：2 项通过，1 项不通过"
    assert out.data == ["results", "report"]


async def test_reference_filters_and_conditions_are_joined_per_side():
    rt, lake, _, calls = await env(Settings(databricks_source_catalog="lake"))
    reconcile = check(
        "vol",
        "reconcile",
        left={"table": "ibf_table2", "expr": "sum(qty)", "where": "month = '2026-10'"},
        right={"table": "prev.sales.forecast", "expr": "sum(value)", "where": "sku = 'A1'"},
    )
    out = await run(node(checks=[reconcile]), state(lake), rt)
    (found,) = out.outputs["results"]
    # The forecast is read with the project's filter AND this side's own condition.
    assert [c[1]["where"] for c in calls] == [
        "month = '2026-10'",
        "(Type = 'Vol.') AND (sku = 'A1')",
    ]
    assert (found["status"], found["actual"], found["expected"]) == ("passed", 10, 10)
    assert found["message"] == "左边 10，右边 10，一致"
    assert out.outputs["ok"] is True and out.branch == "passed"


async def test_table_names_resolve_by_their_parts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        fake_connector.DATABRICKS_TABLES,
        ("lake.shop", "orders"),
        {"comment": None, "columns": COLUMNS, "foreign_keys": [], "rows": RESULT},
    )
    settings = Settings(
        databricks_catalog="main", databricks_schema="dev", databricks_source_catalog="lake"
    )
    rt, lake, _, calls = await env(settings)
    found = [
        check("a", "rows", table="shop.orders", where="qty < 0"),
        check("b", "rows", table="old.jade.table2", where="qty < 0"),
        check("c", "rows", table="ibf_table2", where="qty < 0"),
    ]
    # Unbound: bare names are in the platform's catalog and schema.
    out = await run(node(checks=found), state(lake, project=None), rt)
    tables = [args["table"] for name, args in calls if name == "aggregate"]
    assert tables == ["lake.shop.orders", "old.jade.table2", "main.dev.ibf_table2"]
    statuses = [r["status"] for r in out.outputs["results"]]
    assert statuses == ["failed", "passed", "error"]
    assert out.outputs["results"][2]["message"].startswith("表 main.dev.ibf_table2 不存在")

    rt, lake, _, _ = await env()
    out = await run(node(checks=found), state(lake, project=None), rt)
    messages = [r["message"] for r in out.outputs["results"]]
    assert messages[0] == (
        "平台没有配置 AEP_DATABRICKS_SOURCE_CATALOG，读不到 shop.orders；"
        "请写成 catalog.schema.table"
    )
    assert messages[2] == (
        "平台没有配置 AEP_DATABRICKS_CATALOG、AEP_DATABRICKS_SCHEMA，读不到结果表"
    )

    rt, lake, _, _ = await env(Settings(databricks_deploy="cluster"))
    out = await run(node(checks=found[2:]), state(lake), rt)
    assert out.outputs["results"][0]["message"] == "cluster 方式不落表，读不到结果表"


async def test_rows_counts_and_samples_what_must_not_exist():
    rt, lake, _, calls = await env()
    n = node(checks=[check("neg", "rows", table="ibf_table2", where="qty < 0", severity="warn")])
    out = await run(n, state(lake), rt)
    (found,) = out.outputs["results"]
    assert found["status"] == "failed" and found["actual"] == 1
    assert found["message"] == "有 1 行满足 qty < 0"
    assert found["rows"] == [RESULT[2]]
    assert calls[1] == (
        "sample_rows",
        {"schema": "dev.etl", "table": "ibf_table2", "where": "qty < 0"},
    )
    # Only a warning: the node still passes.
    assert out.outputs["ok"] is True and out.branch == "passed"
    assert out.outputs["summary"] == "1 项检查：1 项不通过（警告）"


async def test_unique_reports_repeated_keys():
    rt, lake, _, calls = await env()
    found = [
        check("by_month", "unique", table="ibf_table2", columns=["month"]),
        check("by_sku", "unique", table="ibf_table2", columns=["sku", "month"]),
    ]
    out = await run(node(checks=found), state(lake), rt)
    month, sku = out.outputs["results"]
    assert (month["status"], month["actual"], month["message"]) == (
        "failed",
        1,
        "按 month 有 1 组重复",
    )
    assert month["rows"] == [{"month": "2026-11", "count": 2}]
    assert (sku["status"], sku["message"]) == ("passed", "按 sku、month 没有重复")
    assert calls[0] == (
        "duplicates",
        {"table": "dev.etl.ibf_table2", "columns": ["month"], "where": None, "limit": 20},
    )


async def test_grouped_checks_line_up_the_groups():
    rt, lake, _, calls = await env()
    grouped = check(
        "by_month",
        "reconcile",
        left={"table": "ibf_table2", "expr": "sum(qty)"},
        right={"table": "old.jade.table2", "expr": "sum(qty)"},
        group_by=["month"],
        tolerance=0.2,
        relative=True,
    )
    positive = check(
        "positive",
        "aggregate",
        table="ibf_table2",
        expr="min(qty)",
        op="gt",
        value=0,
        group_by=["month"],
    )
    out = await run(node(checks=[grouped, positive]), state(lake), rt)
    found, pos = out.outputs["results"]
    # 2026-11: 3 against 6, half off; 2026-10 agrees.
    assert found["status"] == "failed" and found["actual"] == 1
    assert found["rows"] == [{"month": "2026-11", "left": 3, "right": 6}]
    assert found["message"] == "按 month 分 2 组，1 组对不上"
    assert calls[0][1]["limit"] == 10000 and calls[0][1]["group_by"] == ["month"]
    assert pos["rows"] == [{"month": "2026-11", "value": -1}]
    assert pos["message"] == "按 month 分 2 组，1 组不满足 > 0"


async def test_groups_beyond_the_limit_are_an_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(checks, "GROUP_LIMIT", 1)
    rt, lake, _, _ = await env()
    grouped = check(
        "g",
        "aggregate",
        table="ibf_table2",
        expr="sum(qty)",
        op="ge",
        value=0,
        group_by=["month"],
    )
    out = await run(node(checks=[grouped]), state(lake), rt)
    (found,) = out.outputs["results"]
    assert found["status"] == "error"
    assert found["message"] == "分组有 2 组，超过一次能核对的 1 组；请减少分组列或加条件"


async def test_reconcile_tolerance_absolute_and_relative():
    cases = [
        ({}, "failed", "左边 13，右边 16，相差 3，不一致"),
        ({"tolerance": 3}, "passed", "左边 13，右边 16，相差 3，在容差 3 内"),
        ({"tolerance": 2}, "failed", "左边 13，右边 16，相差 3，超过容差 2"),
        (
            {"tolerance": 0.2, "relative": True},
            "passed",
            "左边 13，右边 16，相差 18.75%，在容差 20% 内",
        ),
    ]
    for more, status, message in cases:
        rt, lake, _, _ = await env()
        c = check(
            "r",
            "reconcile",
            left={"table": "ibf_table2", "expr": "sum(qty)"},
            right={"table": "old.jade.table2", "expr": "sum(qty)"},
            **more,
        )
        (found,) = (await run(node(checks=[c]), state(lake), rt)).outputs["results"]
        assert (found["status"], found["message"]) == (status, message), more


async def test_golden_compares_row_by_row():
    rt, lake, _, calls = await env()
    c = check(
        "old",
        "golden",
        table="ibf_table2",
        expected="old.jade.table2",
        keys=["sku", "month"],
        tolerance=0.0001,
        expected_where="qty > 0",
    )
    out = await run(node(checks=[c]), state(lake), rt)
    (found,) = out.outputs["results"]
    assert found["status"] == "failed"
    assert found["actual"] == {
        "missing": 1,
        "extra": 1,
        "different": 1,
        "duplicates": {"table": 0, "expected": 0},
    }
    assert found["expected"] == "old.jade.table2"
    assert found["message"] == "和老系统的结果对不上：少 1 行、多 1 行、1 行不一致"
    assert [r["kind"] for r in found["rows"]] == ["different", "extra", "missing"]
    assert found["rows"][0]["differs"] == ["qty"]  # amount is within the tolerance
    assert calls == [
        (
            "compare",
            {
                "table": "dev.etl.ibf_table2",
                "expected": "old.jade.table2",
                "keys": ["sku", "month"],
                "columns": None,
                "where": None,
                "expected_where": "qty > 0",
                "tolerance": 0.0001,
                "limit": 20,
            },
        )
    ]
    report = out.outputs["report"]
    assert "| 不通过 | 检查 old | 和老系统的结果对不上：少 1 行、多 1 行、1 行不一致 |" in report
    assert "列里的值是“结果表 / 老系统”。" in report
    assert "| 不一致 | B2 | 2026-11 | 4 / 5 | 40 |" in report
    assert "| 少 | D4 | 2026-11 | — / 1 | — / 1 |" in report


async def test_golden_stops_at_repeated_keys(monkeypatch: pytest.MonkeyPatch):
    rt, lake, _, _ = await env()
    on_month = check(
        "mine", "golden", table="ibf_table2", expected="old.jade.table2", keys=["month"]
    )
    out = await run(node(checks=[on_month]), state(lake), rt)
    (found,) = out.outputs["results"]
    assert found["status"] == "failed"
    assert found["message"] == "结果表按 month 有 1 组重复的行，不能逐行对比"
    assert found["rows"] == [
        {"kind": "duplicate", "side": "table", "key": {"month": "2026-11"}, "count": 2}
    ]

    monkeypatch.setitem(
        fake_connector.DATABRICKS_TABLES,
        ("old.jade", "table2"),
        {"comment": None, "columns": COLUMNS, "foreign_keys": [], "rows": OLD + OLD[:1]},
    )
    on_sku = on_month | {"keys": ["sku"]}
    (found,) = (await run(node(checks=[on_sku]), state(lake), rt)).outputs["results"]
    assert found["status"] == "error"
    assert found["message"] == "老系统的结果表按 sku 有重复，不能逐行对比；请换一组键"


async def test_the_node_adds_the_comparison_with_the_old_system():
    rt, lake, _, calls = await env()
    golden = {
        "expected": "{{start.golden}}",
        "table": "{{plan.result_table}}",
        "keys": "{{plan.result_keys}}",
    }
    plan = {"result_table": "ibf_table2", "result_keys": ["sku", "month"]}
    n = node(golden=golden)

    out = await run(n, state(lake, plan=plan) | {"vars": {"start": {"datasource": lake,
                    "golden": "old.jade.table2"}, "plan": plan}}, rt)  # fmt: skip
    (found,) = out.outputs["results"]
    assert (found["id"], found["label"]) == ("golden", "和老系统的结果一致")
    assert found["status"] == "failed" and calls[0][0] == "compare"

    # No old system's table: no comparison.
    for empty in ["", "  ", None]:
        st = {"input": {}, "vars": {"start": {"datasource": lake, "golden": empty}, "plan": plan},
              "project": PROJECT}  # fmt: skip
        out = await run(n, st, rt)
        assert out.outputs["results"] == [] and out.outputs["summary"] == "没有数据检查"
        assert out.outputs["ok"] is True

    # A result table without a primary key cannot be compared row by row.
    st = {"input": {}, "vars": {"start": {"datasource": lake, "golden": "old.jade.table2"},
          "plan": plan | {"result_keys": []}}, "project": PROJECT}  # fmt: skip
    (found,) = (await run(n, st, rt)).outputs["results"]
    assert found["status"] == "error"
    assert found["message"] == "结果表没有主键，不能逐行对比；请在方案里给结果表写 primary_key"


async def test_a_golden_check_uses_the_node_s_old_table_or_is_skipped():
    rt, lake, _, calls = await env()
    c = check("mine", "golden", table="ibf_table2", keys=["sku", "month"], severity="info")
    out = await run(node(checks=[c]), state(lake), rt)
    (found,) = out.outputs["results"]
    assert (found["status"], found["message"]) == ("skipped", "没有指定老系统的结果表")
    assert calls == [] and out.outputs["ok"] is True
    assert out.outputs["summary"] == "1 项检查：1 项跳过"

    n = node(checks=[c], golden={"expected": "old.jade.table2", "table": "ibf_table2",
                                  "keys": ["sku", "month"]})  # fmt: skip
    mine, added = (await run(n, state(lake), rt)).outputs["results"]
    assert mine["expected"] == "old.jade.table2" and mine["status"] == "failed"
    assert added["id"] == "golden"


async def test_only_if_skips_the_checks_without_calling_the_connector():
    rt, lake, _, calls = await env()
    n = node(
        only_if="{{dev_run.ok}}",
        checks=[check("neg", "rows", table="ibf_table2", where="qty < 0")],
    )
    for ok in [False, None, "true", 1]:
        out = await run(n, state(lake, dev_run={"ok": ok}), rt)
        assert out.outputs == {
            "ok": False,
            "results": [],
            "summary": "前一步没有通过，没有做数据检查",
            "report": "前一步没有通过，没有做数据检查",
        }
        assert out.branch == "failed"
    assert calls == []
    out = await run(n, state(lake, dev_run={"ok": True}), rt)
    assert len(out.outputs["results"]) == 1


async def test_included_checks_join_the_node_s_and_must_be_valid():
    rt, lake, _, _ = await env()
    mine = check("neg", "rows", table="ibf_table2", where="qty < 0")
    theirs = check("dup", "unique", table="ibf_table2", columns=["sku"])
    n = node(checks=[mine], include=["{{plan.checks}}"])
    out = await run(n, state(lake, plan={"checks": [theirs]}), rt)
    assert [r["id"] for r in out.outputs["results"]] == ["neg", "dup"]
    out = await run(n, state(lake, plan={"checks": None}), rt)
    assert [r["id"] for r in out.outputs["results"]] == ["neg"]

    for given, message in [
        ("x", "include 第 1 项（{{plan.checks}}）的值不是检查的列表"),
        ([mine], "检查 id neg 重复了"),
        ([theirs | {"id": "golden"}], "检查 id golden 是留给和老系统对比的，请换一个"),
        ([theirs | {"columns": []}], "检查 dup（columns）：列要写 1–20 个列名"),
        ([{"id": "x", "kind": "rows"}], "检查 x（label）：检查的名称要写 1–100 个字符"),
        ([{"kind": "rows"}], "第 2 条检查（id）：" + ID_RULE),
    ]:
        with pytest.raises(NodeFailure) as failed:
            await run(n, state(lake, plan={"checks": given}), rt)
        assert (failed.value.type, failed.value.message) == ("input_error", message), given


async def test_a_check_that_fails_to_run_does_not_stop_the_others():
    rt, lake, _, _ = await env()
    found = [
        check("bad", "aggregate", table="ibf_table2", expr="avg(qty)", op="eq", value=1),
        check("missing", "rows", table="no.such.table", where="qty < 0"),
        check("param", "rows", table="ibf_table2", where="month = ${param.nope}"),
        check("ok", "rows", table="ibf_table2", where="qty < -5"),
    ]
    out = await run(node(checks=found), state(lake), rt)
    results = out.outputs["results"]
    assert [r["status"] for r in results] == ["error", "error", "error", "passed"]
    assert results[0]["message"] == "聚合表达式写法不对：这个假连接器不认 avg(qty)"
    assert results[1]["message"] == "表 no.such.table 不存在，或者没有读权限"
    assert results[2]["message"] == "没有运行参数 nope"
    assert out.outputs["summary"] == "4 项检查：1 项通过，3 项出错"
    assert out.outputs["ok"] is False

    unbound = [check("param", "rows", table="old.jade.table2", where="month = ${param.m}")]
    out = await run(node(checks=unbound), state(lake, project=None), rt)
    assert out.outputs["results"][0]["message"] == (
        "这次运行没有绑定项目，不能用运行参数 ${param.m}"
    )


async def test_the_datasource_must_be_databricks_and_reachable():
    rt, lake, shop, _ = await env()
    with pytest.raises(NodeFailure) as failed:
        await run(node(checks=[]), state(shop), rt)
    assert (failed.value.type, failed.value.message) == ("input_error", NOT_DATABRICKS)

    sources = rt.datasources
    down = await sources.create("断开的湖", "databricks", "http://down.test/mcp")  # type: ignore[union-attr]

    def resolve(url: str):
        raise OSError("connection refused")

    rt.tools = McpTools(1, resolve=resolve)
    n = node(checks=[check("neg", "rows", table="ibf_table2", where="qty < 0")])
    with pytest.raises(NodeFailure) as failed:
        await run(n, state(down.datasource_id), rt)
    assert failed.value.type == "tool_error"
    assert failed.value.message.startswith("连不上数据源「断开的湖」")


def test_the_report_lists_each_check_and_the_examples():
    results = [
        checks.result(check("a", "rows"), "passed", "没有满足 qty < 0 的行"),
        checks.result(
            check("b", "unique", severity="warn"),
            "failed",
            "按 sku 有 1 组重复",
            rows=[{"sku": "A|1", "count": 2}],
        ),
        checks.result(check("c", "golden"), "skipped", "没有指定老系统的结果表"),
    ]
    assert checks.report(results).splitlines() == [
        "## 数据检查",
        "",
        "3 项检查：1 项通过，1 项不通过（警告），1 项跳过",
        "",
        "| 结果 | 检查 | 说明 |",
        "| --- | --- | --- |",
        "| 通过 | 检查 a | 没有满足 qty < 0 的行 |",
        "| 不通过（警告） | 检查 b | 按 sku 有 1 组重复 |",
        "| 跳过 | 检查 c | 没有指定老系统的结果表 |",
        "",
        "### 不通过（警告）：检查 b（1 行例子）",
        "",
        "| sku | count |",
        "| --- | --- |",
        "| A\\|1 | 2 |",
    ]
    mixed = [
        checks.result(check("x", "rows", severity="warn"), "failed", ""),
        checks.result(check("y", "rows", severity="info"), "failed", ""),
        checks.result(check("z", "rows"), "failed", ""),
    ]
    assert checks.summary(mixed) == "3 项检查：3 项不通过（1 项是警告，1 项是提示）"
    assert checks.report([]) == "## 数据检查\n\n没有数据检查"


def test_values_read_as_people_write_them():
    assert [checks.fmt(v) for v in [1385355, 1385355.07, 0.5, 1e-9, None, True, "x"]] == [
        "1,385,355",
        "1,385,355.07",
        "0.5",
        "1e-09",
        "空值",
        "true",
        "x",
    ]


def test_what_a_check_may_say():
    def said(c: Any) -> list[tuple[tuple, str]]:
        return checks.problems(c)

    assert said(check("a", "aggregate", table="t", expr="sum(x)", op="eq", value=1)) == []
    assert said("x") == [((), "每条检查要写成一个对象")]
    assert said({"id": "A", "label": "", "kind": "sql"}) == [
        (("id",), "检查 id 只能用小写字母、数字和下划线，以字母开头，最多 32 个字符"),
        (("label",), "检查的名称要写 1–100 个字符"),
        (("kind",), "检查的 kind 只能是 aggregate、reconcile、unique、golden、rows"),
    ]
    cases = [
        (check("a", "rows", table="t", where="x > 0", sql="1"), ("sql",),
         "rows 检查没有 sql 这一项"),
        (check("a", "rows", table="a.b.c.d", where="x"), ("table",),
         "表名要写成结果表的名字、schema.table（抽取的源表）或 catalog.schema.table，不带反引号"),
        (check("a", "rows", table="`a`.b.c", where="x"), ("table",),
         "表名要写成结果表的名字、schema.table（抽取的源表）或 catalog.schema.table，不带反引号"),
        (check("a", "rows", table="t"), ("where",), "条件不能为空"),
        (check("a", "rows", table="t", where="x; drop"), ("where",), "条件不能有分号"),
        (check("a", "rows", table="t", where="x in (select 1)"), ("where",), "条件不能有 SELECT"),
        (check("a", "rows", table="t", where="x = ${month}"), ("where",),
         "运行参数要写成 ${param.<key>}（key 是项目参数的标识）"),
        (check("a", "rows", table="t", where="x", severity="fatal"), ("severity",),
         "severity 只能是 error、warn、info"),
        (check("a", "aggregate", table="t", expr="sum(x", op="eq", value=1), ("expr",),
         "聚合表达式的括号要配对"),
        (check("a", "aggregate", table="t", expr="sum(x)", op="like", value=1), ("op",),
         "op 只能是 eq、ne、gt、ge、lt、le、between"),
        (check("a", "aggregate", table="t", expr="sum(x)", op="between", value=1), ("value",),
         "between 要写数字 value 和 max"),
        (check("a", "aggregate", table="t", expr="sum(x)", op="between", value=2, max=1),
         ("max",), "max 不能比 value 小"),
        (check("a", "aggregate", table="t", expr="sum(x)", op="gt", value=True), ("value",),
         "value 要是数字或文字"),
        (check("a", "aggregate", table="t", expr="sum(x)", op="eq", value=1, max=2), ("max",),
         "只有 between 要写 max"),
        (check("a", "aggregate", table="t", expr="sum(x)", op="eq", value=1, tolerance=-1),
         ("tolerance",), "tolerance 要是不小于 0 的数"),
        (check("a", "aggregate", table="t", expr="sum(x)", op="eq", value=1,
               group_by=["Value"]), ("group_by",), "分组列不能叫 value（和结果里的 value 重名）"),
        (check("a", "unique", table="t", columns=["a", "A"]), ("columns",), "列有重复的列"),
        (check("a", "unique", table="t", columns=["count"]), ("columns",),
         "列不能叫 count（和结果里的 count 重名）"),
        (check("a", "golden", table="t"), ("keys",), "keys 要写 1–20 个列名"),
        (check("a", "reconcile", left={"table": "t"}, right="x"), ("left", "expr"),
         "聚合表达式不能为空"),
        (check("a", "reconcile", left={"table": "t", "expr": "sum(x)"}, right="x"), ("right",),
         "right 要写成 {table, expr, where}"),
        (check("a", "reconcile", left={"table": "t", "expr": "sum(x)", "on": 1},
               right={"table": "t", "expr": "sum(x)"}, relative="yes"), ("left", "on"),
         "left 没有 on 这一项"),
    ]  # fmt: skip
    for c, loc, message in cases:
        assert (loc, message) in said(c), (c, said(c))


async def test_saving_checks_the_settings():
    rt, lake, shop, _ = await env()
    env_ = CheckEnv(None, MemorySkillStore(), datasources=rt.datasources, tools=rt.tools)

    async def problems(n: dict) -> list[tuple[list, str]]:
        p = Problems(())
        await TYPES["data_check"].check(n, p, env_)
        return p.found

    good = node(
        datasource=lake,
        checks=[
            check("a", "rows", table="ibf_table2", where="qty < 0"),
            # Fields with references are checked when the run has their values.
            check(
                "b",
                "aggregate",
                table="{{plan.result_table}}",
                expr="count(*)",
                op="ge",
                value="{{start.least}}",
            ),  # fmt: skip
        ],
        include=["{{plan.checks}}"],
        golden={"expected": "{{start.golden}}", "table": "t", "keys": "{{plan.result_keys}}"},
        only_if="{{dev_run.ok}}",
    )
    assert await problems(good) == []
    assert await problems(node()) == []

    assert await problems(node(datasource=shop)) == [
        (["datasource"], "数据检查要用 Databricks 数据源")
    ]
    found = await problems(
        node(
            checks=[
                check("a", "rows", table="t", where="x"),
                check("a", "rows", table="t", where="x"),
                check("golden", "rows", table="t", where="x"),
                check("c", "rows", table="t"),
            ],
            include=["{{plan.checks}}", "plan.checks"],
            golden={"expected": 1, "keys": "a", "other": 1},
            only_if="true",
        )
    )
    assert found == [
        (["checks", 1, "id"], "检查 id a 重复了"),
        (["checks", 2, "id"], "检查 id golden 是留给和老系统对比的，请换一个"),
        (["checks", 3, "where"], "条件不能为空"),
        (["include", 1], "每项必须恰好是一个变量，如 {{check_plan.checks}}"),
        (["golden", "other"], "golden 没有 other 这一项"),
        (["golden", "expected"], "golden 的 expected 要是文字或一个变量"),
        (["golden", "keys"], "golden 的 keys 要是列名列表或一个变量"),
        (["only_if"], "only_if 必须恰好是一个变量，如 {{dev_run.ok}}"),
    ]
    many = node(checks=[check(f"c{i}", "rows", table="t", where="x") for i in range(51)])
    assert await problems(many) == [(["checks"], "checks 要是 0–50 条检查的列表")]


async def test_group_keys_line_up_across_number_types():
    # An integer column on one side and a decimal one on the other: 2026 and 2026.0.
    async def call(tool: str, args: dict) -> dict:
        year = 2026 if args["table"] == "a.b.left" else 2026.0
        return {"rows": [{"year": year, "value": 5}], "groups": 1}

    c = check(
        "years",
        "reconcile",
        left={"table": "a.b.left", "expr": "sum(x)"},
        right={"table": "a.b.right", "expr": "sum(x)"},
        group_by=["year"],
    )
    found = await checks.run(c, checks.Scope(None, "", None), call)
    assert (found["status"], found["message"]) == ("passed", "按 year 分 1 组，每组都一致")
