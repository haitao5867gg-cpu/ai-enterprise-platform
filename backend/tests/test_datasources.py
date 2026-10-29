"""Data sources API and the MCP tool client, against an in-process fake connector."""

import httpx
import pytest

from aep.datasources import MemoryDataSourceStore, register_defaults
from aep.main import app
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError
from tests import fake_connector

UNREACHABLE = "http://127.0.0.1:1/mcp"


def tools_for(server=None, timeout_seconds: float = 5) -> McpTools:
    server = server or fake_connector.build()
    return McpTools(
        timeout_seconds, resolve=lambda url: server if url == fake_connector.URL else url
    )


@pytest.fixture
async def api():
    app.state.datasources = MemoryDataSourceStore()
    app.state.mcp_tools = tools_for()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def register(api, name="shop", url=fake_connector.URL) -> dict:
    resp = await api.post(
        "/datasources", json={"name": name, "kind": "postgres", "connector_url": url}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_register_list_and_get(api):
    ds = await register(api)

    assert [d["name"] for d in (await api.get("/datasources")).json()] == ["shop"]
    got = (await api.get(f"/datasources/{ds['datasource_id']}")).json()
    assert got["connector_url"] == fake_connector.URL
    assert got["kind"] == "postgres"


async def test_duplicate_name_is_rejected(api):
    await register(api)
    resp = await api.post(
        "/datasources",
        json={"name": "shop", "kind": "postgres", "connector_url": fake_connector.URL},
    )
    assert resp.status_code == 409


async def test_invalid_input_is_rejected(api):
    bad_kind = {"name": "x", "kind": "oracle", "connector_url": fake_connector.URL}
    bad_url = {"name": "x", "kind": "postgres", "connector_url": "connector:9000"}
    assert (await api.post("/datasources", json=bad_kind)).status_code == 422
    assert (await api.post("/datasources", json=bad_url)).status_code == 422
    assert (await api.get("/datasources/nope")).status_code == 404


async def test_tools_lists_connector_tools(api):
    ds = await register(api)

    resp = await api.get(f"/datasources/{ds['datasource_id']}/tools")

    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert names == {
        "list_schemas",
        "list_tables",
        "describe_table",
        "sample_rows",
        "table_stats",
        "read_rows",
    }
    describe = next(t for t in resp.json() if t["name"] == "describe_table")
    assert describe["input_schema"]["required"] == ["schema", "table"]


async def test_unreachable_connector_is_a_clear_error(api):
    ds = await register(api, url=UNREACHABLE)

    resp = await api.get(f"/datasources/{ds['datasource_id']}/tools")

    assert resp.status_code == 502
    assert "connector unreachable" in resp.json()["detail"]
    assert (await api.get("/datasources")).status_code == 200


async def test_call_returns_plain_data():
    tools = tools_for()

    table = await tools.call(
        fake_connector.URL, "describe_table", {"schema": "sales", "table": "orders"}
    )
    schemas = await tools.call(fake_connector.URL, "list_schemas")

    assert [c["name"] for c in table["columns"]] == [
        "order_id",
        "customer_id",
        "quantity",
        "status",
        "ordered_at",
    ]
    assert schemas == {"schemas": ["sales"]}


async def test_tool_error_is_raised():
    with pytest.raises(ToolError, match="does not exist"):
        await tools_for().call(
            fake_connector.URL, "describe_table", {"schema": "sales", "table": "x"}
        )


async def test_slow_connector_times_out():
    tools = tools_for(fake_connector.build(delay_seconds=2), timeout_seconds=0.2)
    with pytest.raises(ConnectorUnavailable, match="no answer"):
        await tools.call(
            fake_connector.URL, "describe_table", {"schema": "sales", "table": "orders"}
        )


async def test_defaults_are_registered_once():
    store = MemoryDataSourceStore()
    defaults = [{"name": "示例库", "kind": "postgres", "connector_url": fake_connector.URL}]

    assert await register_defaults(store, defaults) == 1
    assert await register_defaults(store, defaults) == 0
    assert [d.name for d in await store.all()] == ["示例库"]
