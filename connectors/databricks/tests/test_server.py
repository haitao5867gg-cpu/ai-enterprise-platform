"""The MCP tools as a client sees them, and the health check."""

import httpx2 as httpx
from mcp import Client
from pydantic import SecretStr

from aep_databricks.executor import Result, StatementError
from aep_databricks.server import create_server
from tests.conftest import FakeExecutor, settings

TOOLS = {
    "list_schemas",
    "list_tables",
    "describe_table",
    "sample_rows",
    "read_rows",
    "table_stats",
    "aggregate",
    "duplicates",
    "compare",
}


async def test_nine_read_only_tools_with_checked_arguments():
    fake = FakeExecutor(lambda s: Result([("city", "string")], [["上海"]]))
    async with Client(create_server(settings(), fake)) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == TOOLS
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools)
        assert all(not tool.annotations.destructive_hint for tool in tools)

        result = await client.call_tool(
            "sample_rows",
            {"schema": "main.sales", "table": "orders", "limit": 3, "where": "city = '上海'"},
        )
        assert not result.is_error
        assert result.structured_content == {
            "schema": "main.sales",
            "table": "orders",
            "rows": [{"city": "上海"}],
        }
        for args in [{"limit": 101}, {"limit": 0}, {"limit": True}, {"limit": "3"}]:
            bad = await client.call_tool(
                "sample_rows", {"schema": "main.sales", "table": "orders", **args}
            )
            assert bad.is_error
        refused = await client.call_tool(
            "sample_rows", {"schema": "main.sales", "table": "orders", "where": "1=1; x"}
        )
        assert refused.is_error
        assert "读取条件写法不对：不能有分号" in refused.content[0].text
        other = await client.call_tool("list_tables", {"schema": "secret.stuff"})
        assert other.is_error and "没有允许读 catalog secret" in other.content[0].text
        tolerance = await client.call_tool(
            "compare",
            {"table": "main.s.a", "expected": "main.s.b", "keys": ["id"], "tolerance": -1},
        )
        assert tolerance.is_error
        lookup = {"table": "main.ref.cities", "on": {"city": "name"}, "found": False}
        looked = await client.call_tool(
            "aggregate", {"table": "main.sales.orders", "expr": "count(*)", "lookup": lookup}
        )
        assert not looked.is_error
        for wrong in [{"found": "false"}, {"found": None}, {"on": {}}, {"extra": 1}]:
            bad = await client.call_tool(
                "aggregate",
                {"table": "main.sales.orders", "expr": "count(*)", "lookup": lookup | wrong},
            )
            assert bad.is_error, wrong
    assert fake.statements == [
        "SELECT * FROM `main`.`sales`.`orders` WHERE (city = '上海') LIMIT 3",
        "SELECT (count(*)) AS value FROM `main`.`sales`.`orders` AS __aep_t WHERE NOT EXISTS "
        "(SELECT 1 FROM `main`.`ref`.`cities` AS __aep_l WHERE __aep_l.`name` <=> "
        "__aep_t.`city`)",
    ]


async def test_without_a_workspace_the_connector_starts_and_says_so():
    bare = settings(databricks_read_host="", databricks_read_token=SecretStr(""))
    async with Client(create_server(bare)) as client:
        result = await client.call_tool("list_schemas", {})
        assert result.is_error
        assert "连接器没有配置 Databricks 工作区" in result.content[0].text


async def get(app, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        return await client.get(path)


async def test_the_health_check_reaches_databricks_only_when_asked():
    fake = FakeExecutor()
    app = create_server(settings(), fake).streamable_http_app()
    plain = await get(app, "/healthz")
    assert plain.status_code == 200
    assert plain.json() == {"status": "ok", "configured": True, "missing": []}
    assert fake.statements == []
    probe = await get(app, "/healthz?probe=1")
    assert probe.status_code == 200 and fake.statements == ["SELECT 1"]

    def broken(statement: str) -> Result:
        raise StatementError("Databricks 报错：token token-for-tests at https://adb-1.test")

    app = create_server(settings(), FakeExecutor(broken)).streamable_http_app()
    failed = await get(app, "/healthz?probe=1")
    assert failed.status_code == 503
    assert failed.json() == {"status": "error", "problem": "Databricks 执行 SELECT 1 失败"}

    bare = settings(databricks_read_host="", databricks_read_catalogs=[])
    app = create_server(bare).streamable_http_app()
    plain = await get(app, "/healthz")
    assert plain.status_code == 200 and plain.json()["configured"] is False
    assert plain.json()["missing"] == ["AEP_DATABRICKS_READ_HOST", "AEP_DATABRICKS_READ_CATALOGS"]
    probe = await get(app, "/healthz?probe=1")
    assert probe.status_code == 503
    assert probe.json()["problem"] == (
        "缺少设置：AEP_DATABRICKS_READ_HOST、AEP_DATABRICKS_READ_CATALOGS"
    )
