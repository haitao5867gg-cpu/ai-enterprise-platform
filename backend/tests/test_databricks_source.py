"""Tables on Databricks as a data source (docs/api/datasources.md "Databricks 数据源", the
platform side; docs/api/data-nodes.md batch 3 for etl_profile and databricks_ingest), on
the fake Databricks connector: names, the project's references and read filters, no
copying, the longer tool timeout, and the code app etl refusing it."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.config import Settings
from aep.datasources import DATA_KINDS, SOURCE_KINDS, MemoryDataSourceStore, tool_seconds
from aep.etl.profile import describe_for_model
from aep.runtime.flow.nodes import TYPES, CheckEnv, NodeFailure, Problems, Runtime
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.skills import MemorySkillStore
from aep.tools.mcp import McpTools, ToolError
from tests import fake_connector
from tests.conftest import SETTINGS, FakeGateway
from tests.test_etl import start

FORECAST = "prev.sales.forecast"
PROJECT = {
    "project_id": "p-1",
    "name": "IBF 预测",
    "references": [
        {
            "name": "PREV.sales.Forecast",  # Unity Catalog names are not case sensitive.
            "label": "预测表",
            "filter": "Type = 'Vol.'",
            "description": "上一期的需求预测，金额和数量各占一半",
        },
        {"name": "prev.sales.sku", "label": "物料", "filter": None, "description": ""},
    ],
}


class Recording(McpTools):
    """McpTools that remembers the timeout of each call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.timeouts: list[tuple[str, float | None]] = []

    async def call(self, url, name, arguments=None, timeout_seconds=None):  # type: ignore[override]
        self.timeouts.append((name, timeout_seconds))
        return await super().call(url, name, arguments, timeout_seconds)


async def lake_env(settings: Settings = SETTINGS) -> tuple[Runtime, str, str, list, Recording]:
    sources = MemoryDataSourceStore()
    lake = await sources.create("数据湖", "databricks", fake_connector.DATABRICKS_URL)
    shop = await sources.create("示例库", "postgres", fake_connector.URL)
    calls: list[tuple[str, dict]] = []
    servers = {
        fake_connector.DATABRICKS_URL: fake_connector.build_databricks(calls),
        fake_connector.URL: fake_connector.build(),
    }
    tools = Recording(5, resolve=lambda url: servers[url])
    rt = Runtime(
        FakeGateway().client,
        "mock",
        {},
        {},
        datasources=sources,
        tools=tools,
        settings=settings,
    )
    return rt, lake.datasource_id, shop.datasource_id, calls, tools


def state(project: dict | None = None, **variables: Any) -> dict:
    found: dict[str, Any] = {"input": {}, "vars": variables, "run_id": "run-1"}
    if project is not None:
        found["project"] = project
    return found


def profile(**changes: Any) -> dict:
    return {
        "id": "profile",
        "type": "etl_profile",
        "title": "探查数据",
        "datasource": "{{start.datasource}}",
        "tables": "{{start.tables}}",
    } | changes


def ingest(**changes: Any) -> dict:
    return {
        "id": "ingest",
        "type": "databricks_ingest",
        "title": "抽取",
        "datasource": "{{start.datasource}}",
        "tables": "{{start.tables}}",
    } | changes


async def run(node: dict, st: dict, rt: Runtime):
    return await TYPES[node["type"]].run(node, st, rt)


def test_databricks_is_a_data_source_whose_tools_return_data():
    assert "databricks" in SOURCE_KINDS and "databricks" in DATA_KINDS
    assert "mcp" not in SOURCE_KINDS


async def test_tables_are_named_with_their_catalog():
    rt, lake, _, _, _ = await lake_env()
    for tables, names in [
        (FORECAST, [FORECAST]),
        ("prev.sales.*", ["prev.sales.forecast", "prev.sales.sku"]),
        ([FORECAST, FORECAST], [FORECAST]),
    ]:
        out = await run(profile(), state(start={"datasource": lake, "tables": tables}), rt)
        assert out.outputs["names"] == names, tables

    for tables, problem in [
        ("sales.forecast", "表 sales.forecast 要写成 catalog.schema.table 或 catalog.schema.*"),
        ("a.b.c.d", "表 a.b.c.d 要写成 catalog.schema.table 或 catalog.schema.*"),
        ("", "Databricks 数据源请写要探查的表"),
    ]:
        with pytest.raises(NodeFailure) as failed:
            await run(profile(), state(start={"datasource": lake, "tables": tables}), rt)
        assert (failed.value.type, failed.value.message) == ("input_error", problem)


async def test_profile_reads_the_project_references_with_their_filters():
    rt, lake, _, calls, _ = await lake_env()
    st = state(PROJECT, start={"datasource": lake, "tables": ""})
    out = await run(profile(), st, rt)

    # Names as the project wrote them; the connector does not mind the case.
    assert out.outputs["names"] == ["PREV.sales.Forecast", "prev.sales.sku"]
    forecast, sku = out.outputs["tables"]
    assert forecast["filter"] == "Type = 'Vol.'" and sku["filter"] is None
    assert forecast["comment"] == "上一期的需求预测" and sku["comment"] is None
    assert [c["comment"] for c in forecast["columns"]] == [
        "物料号",
        None,
        "Vol. 是数量，Amt. 是金额",
        None,
    ]
    # The stats and the sample only count the rows the filter keeps; the whole table's
    # count says how many it leaves out.
    assert forecast["estimated_row_count"] == 2
    assert (forecast["unfiltered_row_count"], sku["unfiltered_row_count"]) == (3, None)
    assert {r["Type"] for r in forecast["sample"]} == {"Vol."}
    wheres = {(name, args["table"].lower()): args.get("where") for name, args in calls}
    assert wheres[("table_stats", "forecast")] == "Type = 'Vol.'"
    assert wheres[("sample_rows", "forecast")] == "Type = 'Vol.'"
    assert wheres[("table_stats", "sku")] is None
    assert wheres[("aggregate", "prev.sales.forecast")] is None
    assert [a["expr"] for n, a in calls if n == "aggregate"] == ["count(*)"]

    text = out.outputs["description"]
    assert "表注释：上一期的需求预测" in text
    assert "项目引用表「预测表」：上一期的需求预测，金额和数量各占一半" in text
    said = "只读 Type = 'Vol.' 的行（项目登记的读取筛选，统计和样本也只算这些行"
    assert f"{said}；全表 3 行，筛掉了 1 行）" in text
    # Without counts (outbound policy "schema") the filter is still said, the rows are not.
    assert f"{said}）" in describe_for_model(out.outputs["tables"], "schema")
    assert "- sku：string，非空，空值率 0%，不同值 2；说明：物料号" in text
    # Named tables that the project references get their filters too.
    named = state(PROJECT, start={"datasource": lake, "tables": FORECAST})
    assert (await run(profile(), named, rt)).outputs["tables"][0]["filter"] == "Type = 'Vol.'"


PLACED = PROJECT | {
    "table_prefix": "ibf_",
    "environment": {"key": "dev", "label": "开发", "catalog": "prev", "schema": "Jade"},
}
RESULT = {
    "comment": "结果表",
    "columns": [
        fake_connector._col("sku", "string", nullable=False) | {"comment": "物料号"},
        fake_connector._col("qty", "decimal(18,4)") | {"comment": None},
    ],
    "foreign_keys": [],
    "rows": [{"sku": "A1", "qty": 1}],
}


async def test_profile_lists_the_projects_tables_in_its_environment(monkeypatch):
    tables = dict(fake_connector.DATABRICKS_TABLES)
    tables[("prev.jade", "ibf_table2")] = RESULT
    tables[("prev.jade", "other_table")] = RESULT  # not the project's prefix
    monkeypatch.setattr(fake_connector, "DATABRICKS_TABLES", tables)
    rt, lake, shop, calls, _ = await lake_env()
    out = await run(profile(), state(PLACED, start={"datasource": lake, "tables": ""}), rt)
    assert out.outputs["existing"] == [
        {
            "name": "ibf_table2",
            "table": "prev.Jade.ibf_table2",
            "columns": [
                {"name": "sku", "data_type": "string", "nullable": False, "comment": "物料号"},
                {"name": "qty", "data_type": "decimal(18,4)", "nullable": True, "comment": None},
            ],
        }
    ]
    # Only the structure: no rows, no stats of the project's own tables.
    assert ("list_tables", {"schema": "prev.Jade"}) in calls
    assert not [a for n, a in calls if n != "describe_table" and a.get("table") == "ibf_table2"]
    text = out.outputs["existing_description"]
    assert text.startswith("环境「开发」的 `prev.Jade`里已经有下面这些本项目的表。")
    assert "columns 要写出表里已有的全部列" in text
    assert (
        "表 ibf_table2（2 列）\n  - sku：string，非空；说明：物料号\n  - qty：decimal(18,4)" in text
    )
    assert "ibf_table2" not in out.outputs["description"]  # not a source
    assert out.outputs["summary"].endswith("；项目在环境里已有的表：ibf_table2")

    # None yet; listing fails (the plan then adds nothing); not a Databricks source.
    del tables[("prev.jade", "ibf_table2")]
    rt, lake, shop, calls, _ = await lake_env()
    out = await run(profile(), state(PLACED, start={"datasource": lake, "tables": ""}), rt)
    assert out.outputs["existing"] == []
    assert out.outputs["existing_description"] == (
        "环境「开发」的 `prev.Jade`里还没有本项目的表，目标表都会新建。"
    )

    real = rt.tools.call

    async def refusing(url, name, arguments=None, timeout_seconds=None):  # type: ignore[no-untyped-def]
        if name == "list_tables":
            raise ToolError("catalog prev 不在白名单里")
        return await real(url, name, arguments, timeout_seconds)

    monkeypatch.setattr(rt.tools, "call", refusing)
    out = await run(profile(), state(PLACED, start={"datasource": lake, "tables": ""}), rt)
    assert out.outputs["existing"] is None
    assert "没能列出项目在环境里已有的表：" in out.outputs["summary"]
    assert out.outputs["existing_description"].startswith("（平台没有列出环境「开发」")
    monkeypatch.undo()

    rt, lake, shop, calls, _ = await lake_env()
    st = state(
        PLACED | {"references": [{"name": "sales.orders", "filter": None}]},
        start={"datasource": shop, "tables": ""},
    )
    out = await run(profile(), st, rt)
    assert out.outputs["existing"] is None and calls == []
    # Unbound: nothing said.
    out = await run(profile(), state(start={"datasource": lake, "tables": FORECAST}), rt)
    assert (out.outputs["existing"], out.outputs["existing_description"]) == (None, "")


async def test_a_project_without_references_needs_the_tables_named(monkeypatch):
    rt, lake, _, _, _ = await lake_env()
    bare = PROJECT | {"references": []}
    with pytest.raises(NodeFailure, match="项目「IBF 预测」没有登记引用表"):
        await run(profile(), state(bare, start={"datasource": lake, "tables": ""}), rt)
    monkeypatch.setattr("aep.runtime.flow.data_nodes.MAX_TABLES", 1)
    with pytest.raises(NodeFailure, match="一共 2 张表，一次最多 1 张"):
        await run(profile(), state(PROJECT, start={"datasource": lake, "tables": ""}), rt)


async def test_database_tables_keep_their_names_and_get_no_filter():
    rt, _, shop, calls, _ = await lake_env()
    st = state(
        PROJECT | {"references": [{"name": "sales.orders", "filter": "status = 'paid'"}]},
        start={"datasource": shop, "tables": ""},
    )
    out = await run(profile(), st, rt)
    assert out.outputs["names"] == ["sales.customers", "sales.orders"]
    assert all(t["filter"] is None and t["comment"] is None for t in out.outputs["tables"])
    assert calls == []  # The Databricks connector was not asked.


async def test_databricks_tools_get_the_longer_timeout():
    rt, lake, shop, _, tools = await lake_env(Settings(databricks_tool_seconds=240))
    await run(profile(), state(start={"datasource": lake, "tables": FORECAST}), rt)
    assert tools.timeouts and all(t == 240 for _, t in tools.timeouts)
    tools.timeouts.clear()
    await run(profile(), state(start={"datasource": shop, "tables": "sales.orders"}), rt)
    assert tools.timeouts and all(t is None for _, t in tools.timeouts)
    tools.timeouts.clear()
    tool = {"id": "t", "type": "tool", "datasource": lake, "tool": "list_schemas", "args": {}}
    await run(tool, state(), rt)
    assert tools.timeouts == [("list_schemas", 240)]

    lake_source = await rt.datasources.get(lake)  # type: ignore[union-attr]
    shop_source = await rt.datasources.get(shop)  # type: ignore[union-attr]
    assert tool_seconds(lake_source, SETTINGS) == 180  # type: ignore[arg-type]
    assert tool_seconds(shop_source, SETTINGS) is None  # type: ignore[arg-type]


async def test_ingest_reads_databricks_tables_in_place():
    # No Databricks workspace is configured: nothing is copied, so none is needed.
    rt, lake, _, calls, _ = await lake_env()
    st = state(start={"datasource": lake, "tables": "prev.sales.*"})
    out = await run(ingest(max_rows=1, reuse=True), st, rt)
    assert out.outputs == {
        "catalog": None,
        "tables": [
            {
                "source": "prev.sales.forecast",
                "target": "prev.sales.forecast",
                "rows": None,
                "columns": 4,
                "copied": False,
            },
            {
                "source": "prev.sales.sku",
                "target": "prev.sales.sku",
                "rows": None,
                "columns": 1,
                "copied": False,
            },
        ],
        "summary": "这 2 张表本来就在 Databricks 上，直接读，不复制",
        "reused": False,
    }
    assert out.messages[0]["content"].endswith("：`prev.sales.forecast`、`prev.sales.sku`")
    assert out.private is None
    assert {name for name, _ in calls} == {"list_tables", "describe_table"}  # No rows read.


async def test_a_catalog_name_with_a_dash_is_quoted_in_the_target(monkeypatch):
    monkeypatch.setitem(
        fake_connector.DATABRICKS_TABLES,
        ("icsd-ace-dev.sales", "orders"),
        fake_connector.DATABRICKS_TABLES[("prev.sales", "sku")],
    )
    rt, lake, _, _, _ = await lake_env()
    st = state(start={"datasource": lake, "tables": "icsd-ace-dev.sales.orders"})
    out = await run(ingest(), st, rt)
    assert out.outputs["tables"][0]["target"] == "`icsd-ace-dev`.sales.orders"


async def test_saving_accepts_databricks_sources_and_three_part_names():
    rt, lake, _, _, _ = await lake_env()
    env = CheckEnv(None, MemorySkillStore(), datasources=rt.datasources, tools=rt.tools)
    for kind in ["etl_profile", "databricks_ingest"]:
        p = Problems(())
        node = {"id": "n", "type": kind, "datasource": lake, "tables": "prev.sales.forecast"}
        await TYPES[kind].check(node, p, env)
        assert p.found == [], kind
        p = Problems(())
        await TYPES[kind].check(node | {"tables": "a.b.c.d"}, p, env)
        assert [message for _, message in p.found] == [
            "a.b.c.d 要写成 schema.table 或 schema.*（Databricks 的表写成 catalog.schema.table）"
        ]


async def test_the_etl_code_app_sends_databricks_tables_to_the_flow_app():
    sources = MemoryDataSourceStore()
    lake = await sources.create("数据湖", "databricks", fake_connector.DATABRICKS_URL)
    service = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(FakeGateway().client, SETTINGS, datasources=sources),
    )
    record = (await service.get(await start(service, lake.datasource_id))).record
    assert record.error == (
        "「数据湖」是 Databricks 数据源，ETL 应用要选数据库或上传文件的数据源，"
        "Databricks 的表请用编排应用 etl-lakehouse 第 3 版"
    )
