"""Data sources and tool services API (docs/api/datasources.md) and the MCP tool client,
against in-process fake connectors and scripted HTTP replies; nothing leaves the process."""

import io
import socket
from collections import Counter
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import httpx2
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from aep.config import Settings, get_settings
from aep.connector_check import PROBLEM_TEXT, ConnectorCheck
from aep.connector_urls import TEXT
from aep.datasources import (
    ETL_TOOLS,
    DuplicateName,
    MemoryDataSourceStore,
    register_defaults,
)
from aep.health import DEMO_CONNECTOR_URL, demo_checks, find_demo_source
from aep.main import create_app
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow.data_nodes import INGEST_TOOLS, PROFILE_TOOLS
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError
from tests import fake_connector

READ_ONLY = ToolAnnotations(read_only_hint=True)
CONNECTION_WORDS = {
    "timeout": "超时：",
    "refused": "连接被拒绝：",
    "not_mcp": "不是 MCP 服务：",
    "other": "网络不通：",
}


def partial_server() -> MCPServer:
    """4 of the ETL tools, two of them saying they only read."""
    server = MCPServer("partial")

    @server.tool(annotations=READ_ONLY)
    def list_schemas() -> dict:
        """Schemas."""
        return {"schemas": ["sales"]}

    @server.tool(annotations=READ_ONLY)
    def list_tables(schema: str) -> dict:
        """Tables."""
        return {"schema": schema, "tables": []}

    @server.tool()
    def describe_table(schema: str, table: str) -> dict:
        """Columns."""
        return {}

    @server.tool()
    def sample_rows(schema: str, table: str, limit: int = 10) -> dict:
        """Rows."""
        return {}

    return server


def ticket_server() -> MCPServer:
    """A tool service: no ETL tools at all."""
    server = MCPServer("tickets")

    @server.tool(annotations=READ_ONLY)
    def search_tickets(query: str) -> dict:
        """Find tickets."""
        return {"tickets": []}

    @server.tool()
    def get_customer(customer_id: str) -> dict:
        """Look up a customer."""
        return {}

    return server


async def html(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, text="<html>登录</html>", headers={"content-type": "text/html"})


async def not_found(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(404, text="Not Found")


async def misdirected(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(421, text="Invalid Host header")


async def refused(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("All connection attempts failed")


async def no_such_host(request: httpx2.Request) -> httpx2.Response:
    cause = socket.gaierror(-2, "Name or service not known")
    raise httpx2.ConnectError("[Errno -2] Name or service not known") from cause


async def slow(request: httpx2.Request) -> httpx2.Response:
    await anyio.sleep(5)
    return httpx2.Response(200)


async def to_metadata(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(307, headers={"location": "http://169.254.169.254/latest/mcp"})


class Connectors:
    """Each connector address answered in process: a fake MCP server, or scripted HTTP
    replies (anything unknown refuses the connection). Counts connections per host."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.servers: dict[str, Any] = {
            "connector.test": fake_connector.build(),
            "copy.connector.test": fake_connector.build(),
            "partial.connector.test": partial_server(),
            "tools.connector.test": ticket_server(),
            "empty.connector.test": MCPServer("empty"),
        }
        self.handlers = {
            "timeout.connector.test": slow,
            "html.connector.test": html,
            "h404.connector.test": not_found,
            "h421.connector.test": misdirected,
            "nowhere.connector.test": no_such_host,
            "redirect.connector.test": to_metadata,
        }

    def __call__(self, url: str) -> Any:
        host = httpx2.URL(url).host
        self.calls[host] += 1
        if host in self.servers:
            return self.servers[host]
        handler = self.handlers.get(host, refused)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        return streamable_http_client(url, http_client=client)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


@dataclass
class Platform:
    api: httpx.AsyncClient
    app: FastAPI
    sources: MemoryDataSourceStore
    connectors: Connectors
    service: RunService
    settings: Settings
    now: list[float]

    async def register(self, name="shop", url=fake_connector.URL, kind="postgres", **more) -> dict:
        body = {"name": name, "kind": kind, "connector_url": url, **more}
        resp = await self.api.post("/datasources", json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test(self, url: str, kind: str = "postgres") -> dict:
        resp = await self.api.post("/datasources/test", json={"kind": kind, "connector_url": url})
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.fixture
async def platform(tmp_path, gateway):
    app = create_app()
    settings = Settings(
        connector_hosts=["connector.test", "*.connector.test:*", "127.0.0.1:1"],
        uploads_dir=tmp_path,
        files_connector_url="http://files.test:8003",
        connector_check_seconds=0.3,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    sources = MemoryDataSourceStore()
    connectors = Connectors()
    tools = McpTools(2, resolve=connectors)
    now = [1000.0]
    app.state.datasources = sources
    app.state.mcp_tools = tools
    app.state.connector_check = ConnectorCheck(clock=lambda: now[0])
    service = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(gateway.client, settings, datasources=sources, tools=tools),
    )
    app.state.run_service = service
    app.state.catalog = AppCatalog(service, sources, settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Platform(client, app, sources, connectors, service, settings, now)
    await gateway.client.close()


# Registering


async def test_register_tests_first_then_lists_and_gets(platform):
    ds = await platform.register(url="HTTP://Connector.Test:80/mcp", description="订单和客户")

    assert platform.connectors.calls == {"connector.test": 1}
    assert ds["connector_url"] == fake_connector.URL  # saved normalised
    assert (await platform.api.get("/datasources")).json() == [ds]
    got = (await platform.api.get(f"/datasources/{ds['datasource_id']}")).json()
    assert got == ds
    assert (got["name"], got["kind"], got["description"]) == ("shop", "postgres", "订单和客户")


async def test_test_endpoint_shape(platform):
    result = await platform.test(fake_connector.URL)

    assert set(result) == {"ok", "latency_ms", "tools", "missing_tools", "problems"}
    assert result["ok"] is True and isinstance(result["latency_ms"], int)
    assert result["missing_tools"] == [] and result["problems"] == []
    assert {t["name"] for t in result["tools"]} == set(ETL_TOOLS)
    assert all(set(t) == {"name", "description", "read_only"} for t in result["tools"])
    assert not any(t["read_only"] for t in result["tools"])  # the fake does not say
    assert (await platform.test(fake_connector.URL, "mcp"))["ok"]


async def test_read_only_comes_from_annotations(platform):
    url = "http://partial.connector.test/mcp"
    result = await platform.test(url, "mcp")
    assert result["ok"]
    marked = {t["name"]: t["read_only"] for t in result["tools"]}
    assert marked == {
        "list_schemas": True,
        "list_tables": True,
        "describe_table": False,
        "sample_rows": False,
    }
    ds = await platform.sources.create("工单", "mcp", url)
    tools = (await platform.api.get(f"/datasources/{ds.datasource_id}/tools")).json()
    assert {t["name"]: t["read_only"] for t in tools} == marked
    describe = next(t for t in tools if t["name"] == "describe_table")
    assert describe["input_schema"]["required"] == ["schema", "table"]


async def test_database_kinds_need_the_six_etl_tools(platform):
    url = "http://partial.connector.test/mcp"
    result = await platform.test(url, "postgres")

    assert not result["ok"]
    assert result["missing_tools"] == ["table_stats", "read_rows"]
    [problem] = result["problems"]
    assert problem["code"] == "missing_tools"
    assert problem["message"] == PROBLEM_TEXT["missing_tools"].format(
        tools="table_stats、read_rows"
    )
    assert problem["message"].startswith("缺少 ETL 需要的工具：table_stats、read_rows。")
    assert "「工具服务」" in problem["message"]
    assert (await platform.test(url, "mcp"))["ok"]
    resp = await platform.api.post(
        "/datasources", json={"name": "半个库", "kind": "postgres", "connector_url": url}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "测试连接没有通过。" + problem["message"]
    assert (await platform.register("工单", url, "mcp"))["kind"] == "mcp"


async def test_tool_service_needs_a_tool(platform):
    url = "http://empty.connector.test/mcp"
    result = await platform.test(url, "mcp")
    assert result["problems"] == [{"code": "no_tools", "message": PROBLEM_TEXT["no_tools"]}]
    assert result["missing_tools"] == [] and result["latency_ms"] is not None
    as_database = await platform.test(url, "mysql")
    assert as_database["missing_tools"] == list(ETL_TOOLS)
    assert [p["code"] for p in as_database["problems"]] == ["missing_tools"]
    tickets = await platform.test("http://tools.connector.test/mcp", "mcp")
    assert tickets["ok"] and [t["name"] for t in tickets["tools"]] == [
        "search_tickets",
        "get_customer",
    ]


@pytest.mark.parametrize(
    ("url", "code", "text"),
    [
        ("http://elsewhere.example/mcp", "host_not_allowed", None),
        ("http://u:p@connector.test/mcp", "credentials", "credentials"),
        ("http://connector.test/mcp?token=x", "credentials", "credentials"),
        ("http://sandbox-relay:8004/mcp", "internal_host", "internal_host"),
        ("http://169.254.169.254/latest", "metadata", "metadata"),
        ("http://connector.test/mcp#x", "bad_url", "bad_url"),
        ("postgresql://reader:pw@db:5432/sales", "bad_url", "database_url"),
    ],
)
async def test_rejected_address_is_never_probed(platform, url, code, text):
    result = await platform.test(url)

    assert result == {
        "ok": False,
        "latency_ms": None,
        "tools": [],
        "missing_tools": [],
        "problems": [{"code": code, "message": result["problems"][0]["message"]}],
    }
    if text:
        assert result["problems"][0]["message"] == TEXT[text]
    resp = await platform.api.post(
        "/datasources", json={"name": "x", "kind": "postgres", "connector_url": url}
    )
    assert resp.status_code == 422 and resp.json()["detail"].startswith("测试连接没有通过。")
    assert platform.connectors.total == 0


@pytest.mark.parametrize(
    ("host", "code"),
    [
        ("timeout.connector.test", "timeout"),
        ("refused.connector.test", "refused"),
        ("html.connector.test", "not_mcp"),
        ("h404.connector.test", "not_mcp"),
        ("h421.connector.test", "not_mcp"),
        ("redirect.connector.test", "not_mcp"),
        ("nowhere.connector.test", "other"),
    ],
)
async def test_connection_problems_are_categories_only(platform, host, code):
    resp = await platform.api.post(
        "/datasources/test", json={"kind": "postgres", "connector_url": f"http://{host}/mcp"}
    )

    result = resp.json()
    assert result["problems"] == [
        {"code": code, "message": PROBLEM_TEXT[code].format(seconds="0.3")}
    ]
    assert result["problems"][0]["message"].startswith(CONNECTION_WORDS[code])
    assert result["latency_ms"] is None and result["tools"] == []
    for raw in ("http://", "connector.test", "Errno", "MCPError", "All connection", "Unexpected"):
        assert raw not in resp.text
    assert platform.connectors.calls[host] == 1
    assert "169.254.169.254" not in platform.connectors.calls


def test_timeout_text_has_whole_seconds():
    assert (
        PROBLEM_TEXT["timeout"]
        .format(seconds=f"{5.0:g}")
        .startswith("超时：连接器在 5 秒内没有响应。")
    )


async def test_create_with_failed_test_is_422_chinese_and_saves_nothing(platform):
    resp = await platform.api.post(
        "/datasources",
        json={
            "name": "shop",
            "kind": "postgres",
            "connector_url": "http://refused.connector.test/",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "测试连接没有通过。" + PROBLEM_TEXT["refused"]
    assert (await platform.api.get("/datasources")).json() == []


async def test_duplicate_name_is_409_chinese(platform):
    await platform.register()
    probes = platform.connectors.total
    resp = await platform.api.post(
        "/datasources",
        json={"name": " shop ", "kind": "mcp", "connector_url": "http://refused.connector.test/"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "已经有叫「shop」的数据源或工具服务了"
    assert platform.connectors.total == probes  # refused before testing
    upload = await platform.api.post(
        "/datasources/files",
        data={"name": "shop"},
        files=[("files", ("a.csv", io.BytesIO(b"a,b\n1,2\n")))],
    )
    assert upload.status_code == 409
    assert upload.json()["detail"] == "已经有叫「shop」的数据源或工具服务了"


async def test_invalid_input_is_chinese_422(platform):
    base = {"name": "x", "kind": "postgres", "connector_url": fake_connector.URL}
    cases = [
        ({**base, "kind": "oracle"}, "「类型」的格式、长度或范围不正确"),
        ({**base, "name": "  "}, "「名称」的格式、长度或范围不正确"),
        ({**base, "name": "名" * 101}, "「名称」的格式、长度或范围不正确"),
        ({"name": "x", "kind": "postgres"}, "「连接器地址」的格式、长度或范围不正确"),
        ({**base, "description": "说" * 2001}, "「说明」的格式、长度或范围不正确"),
        ({**base, "description": None}, "请求字段不能为 null"),
        # Postgres text cannot hold NUL: a format error, not a 500.
        ({**base, "name": "x\u0000"}, "「名称」的格式、长度或范围不正确"),
        ({**base, "description": "a\u0000b"}, "「说明」的格式、长度或范围不正确"),
    ]
    for body, detail in cases:
        resp = await platform.api.post("/datasources", json=body)
        assert (resp.status_code, resp.json()["detail"]) == (422, detail), body
    resp = await platform.api.post("/datasources/test", json={"kind": "x", "connector_url": ""})
    assert resp.json()["detail"] == "「类型」的格式、长度或范围不正确"
    # A URL with no pattern to match: the address rules answer, with a 200.
    assert (await platform.test("connector:9000"))["problems"][0]["code"] == "bad_url"
    for path in ("", "/health", "/usage", "/tools"):
        resp = await platform.api.get(f"/datasources/nope{path}")
        assert (resp.status_code, resp.json()["detail"]) == (404, "没有找到这个数据源或工具服务")
    resp = await platform.api.patch("/datasources/nope", json={"name": "x"})
    assert (resp.status_code, resp.json()["detail"]) == (404, "没有找到这个数据源或工具服务")
    resp = await platform.api.patch("/datasources/nope", json={"description": "\u0000"})
    assert (resp.status_code, resp.json()["detail"]) == (422, "「说明」的格式、长度或范围不正确")


async def test_file_kind_is_upload_only(platform):
    body = {"kind": "file", "connector_url": "http://files.test:8003/datasets/x/mcp"}
    for path, extra in (("/datasources/test", {}), ("/datasources", {"name": "上传"})):
        resp = await platform.api.post(path, json={**body, **extra})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "上传文件的数据源请用「上传文件」登记，不用填地址"
    assert platform.connectors.total == 0


# Editing


async def test_patch_name_and_description_without_test(platform):
    ds = await platform.register()
    probes = platform.connectors.total

    resp = await platform.api.patch(
        f"/datasources/{ds['datasource_id']}",
        json={"name": " 线上订单库 ", "description": "只读副本"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {**ds, "name": "线上订单库", "description": "只读副本"}
    assert platform.connectors.total == probes
    assert [d["name"] for d in (await platform.api.get("/datasources")).json()] == ["线上订单库"]


async def test_patch_rename_to_taken_name_is_409_and_own_name_is_ok(platform):
    a = await platform.register("a")
    b = await platform.register("b", "http://copy.connector.test/mcp")
    probes = platform.connectors.total

    taken = await platform.api.patch(f"/datasources/{b['datasource_id']}", json={"name": "a"})
    assert (taken.status_code, taken.json()["detail"]) == (409, "已经有叫「a」的数据源或工具服务了")
    both = await platform.api.patch(
        f"/datasources/{b['datasource_id']}",
        json={"name": "a", "connector_url": "http://tools.connector.test/mcp"},
    )
    assert both.status_code == 409 and platform.connectors.total == probes  # before testing
    own = await platform.api.patch(f"/datasources/{a['datasource_id']}", json={"name": "a"})
    assert own.status_code == 200
    with pytest.raises(DuplicateName):
        await platform.sources.update(b["datasource_id"], name="a")
    assert await platform.sources.update("nope", name="c") is None


async def test_patch_url_retests(platform):
    ds = await platform.register()
    path = f"/datasources/{ds['datasource_id']}"

    failed = await platform.api.patch(path, json={"connector_url": "http://refused.connector.test"})
    assert failed.status_code == 422
    assert failed.json()["detail"] == "测试连接没有通过。" + PROBLEM_TEXT["refused"]
    tool_service = await platform.api.patch(
        path, json={"name": "新名字", "connector_url": "http://tools.connector.test/mcp"}
    )
    assert tool_service.status_code == 422 and "缺少 ETL 需要的工具" in tool_service.text
    assert (await platform.api.get(path)).json() == ds  # nothing changed, not even the name

    moved = await platform.api.patch(path, json={"connector_url": "HTTP://Copy.Connector.Test/mcp"})
    assert moved.status_code == 200
    assert moved.json()["connector_url"] == "http://copy.connector.test/mcp"
    probes = platform.connectors.total
    same = await platform.api.patch(
        path, json={"connector_url": "http://copy.connector.test:80/mcp/"}
    )
    assert same.status_code == 200 and platform.connectors.total == probes


async def test_patch_rejects_kind_and_file_url(platform):
    ds = await platform.register()
    resp = await platform.api.patch(f"/datasources/{ds['datasource_id']}", json={"kind": "mysql"})
    assert (resp.status_code, resp.json()["detail"]) == (422, "类型登记后不能修改")
    resp = await platform.api.patch(f"/datasources/{ds['datasource_id']}", json={"name": None})
    assert (resp.status_code, resp.json()["detail"]) == (422, "请求字段不能为 null")

    upload = await platform.sources.create("上传", "file", "http://files.test:8003/datasets/x/mcp")
    path = f"/datasources/{upload.datasource_id}"
    moved = await platform.api.patch(path, json={"connector_url": fake_connector.URL})
    assert (moved.status_code, moved.json()["detail"]) == (422, "上传文件的数据源不能修改地址")
    renamed = await platform.api.patch(path, json={"name": "本月订单"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "本月订单"
    assert platform.connectors.total == 1


async def test_legacy_sources_still_listed_and_editable(platform):
    sandbox = await platform.sources.create("旧沙箱", "sqlserver", "http://sandbox-relay:8004/mcp")
    secret = await platform.sources.create(
        "旧报表", "mysql", "http://u:p@old.connector.test/mcp?token=abc"
    )
    await platform.sources.create("坏地址", "mysql", "http://[::1/mcp")
    await platform.sources.create("坏端口", "mysql", "http://h:99999/mcp?token=abc")

    listed = await platform.api.get("/datasources")
    assert listed.status_code == 200
    urls = {d["name"]: d["connector_url"] for d in listed.json()}
    assert urls == {
        "旧沙箱": "http://sandbox-relay:8004/mcp",
        "旧报表": "http://***@old.connector.test/mcp?token=***",
        "坏地址": "http://[::1/mcp",
        "坏端口": "http://h:99999/mcp?token=***",
    }
    path = f"/datasources/{secret.datasource_id}"
    assert (await platform.api.get(path)).json()["connector_url"] == urls["旧报表"]
    assert (await platform.api.patch(path, json={"name": "报表库"})).status_code == 200
    raw = await platform.api.patch(path, json={"connector_url": secret.connector_url})
    assert raw.status_code == 200 and platform.connectors.total == 0
    masked = await platform.api.patch(path, json={"connector_url": urls["旧报表"]})
    assert masked.status_code == 422 and TEXT["credentials"] in masked.json()["detail"]
    assert (await platform.sources.get(secret.datasource_id)).connector_url == secret.connector_url

    # No address rules once registered: health still asks the connector.
    health = (await platform.api.get(f"/datasources/{sandbox.datasource_id}/health")).json()
    assert health["error"] == "refused" and platform.connectors.calls["sandbox-relay"] == 1
    # Nothing that reads stored addresses trips over the odd ones.
    defaults = [{"name": "示例库", "kind": "postgres", "connector_url": fake_connector.URL}]
    assert await register_defaults(platform.sources, defaults) == 1
    registered = await platform.sources.all()
    assert find_demo_source(registered, lambda d: (d.name, d.kind, d.connector_url)) is None


# Health


async def test_health_cache(platform):
    ds = await platform.register()
    path = f"/datasources/{ds['datasource_id']}/health"
    probes = platform.connectors.total

    first = (await platform.api.get(path)).json()  # the test when registering
    assert platform.connectors.total == probes
    assert first["ok"] and first["tool_count"] == 6 and isinstance(first["latency_ms"], int)
    assert first["error"] is None and first["message"] is None and first["checked_at"]
    platform.now[0] += 59
    assert (await platform.api.get(path)).json() == first
    assert platform.connectors.total == probes
    platform.now[0] += 2
    second = (await platform.api.get(path)).json()
    assert platform.connectors.total == probes + 1 and second["checked_at"] != first["checked_at"]
    fresh = (await platform.api.get(path, params={"fresh": "true"})).json()
    assert platform.connectors.total == probes + 2 and fresh["checked_at"] != second["checked_at"]

    down = await platform.sources.create("旧报表库", "mysql", "http://refused.connector.test/mcp")
    down_path = f"/datasources/{down.datasource_id}/health"
    failed = (await platform.api.get(down_path)).json()
    assert failed == {
        "ok": False,
        "latency_ms": None,
        "tool_count": None,
        "error": "refused",
        "message": PROBLEM_TEXT["refused"],
        "checked_at": failed["checked_at"],
    }
    assert (await platform.api.get(down_path)).json() == failed  # failures are kept too
    assert platform.connectors.calls["refused.connector.test"] == 1

    # A new address: its test is the first health, the old result is gone.
    moved = await platform.api.patch(
        f"/datasources/{down.datasource_id}",
        json={"connector_url": "http://copy.connector.test/mcp"},
    )
    assert moved.status_code == 200
    probes = platform.connectors.total
    healthy = (await platform.api.get(down_path)).json()
    assert healthy["ok"] and healthy["tool_count"] == 6 and platform.connectors.total == probes


async def test_tools_502_is_a_category_and_updates_health(platform):
    down = await platform.sources.create("旧报表库", "mysql", "http://refused.connector.test/mcp")

    resp = await platform.api.get(f"/datasources/{down.datasource_id}/tools")

    assert (resp.status_code, resp.json()["detail"]) == (502, PROBLEM_TEXT["refused"])
    health = (await platform.api.get(f"/datasources/{down.datasource_id}/health")).json()
    assert health["error"] == "refused" and platform.connectors.total == 1
    slow_source = await platform.sources.create("慢", "mysql", "http://timeout.connector.test/mcp")
    resp = await platform.api.get(f"/datasources/{slow_source.datasource_id}/tools")
    assert resp.json()["detail"] == PROBLEM_TEXT["timeout"].format(seconds="0.3")

    up = await platform.sources.create("示例库", "postgres", fake_connector.URL)
    tools = await platform.api.get(f"/datasources/{up.datasource_id}/tools")
    assert tools.status_code == 200 and len(tools.json()) == 6
    probes = platform.connectors.total
    health = (await platform.api.get(f"/datasources/{up.datasource_id}/health")).json()
    assert health["ok"] and health["tool_count"] == 6 and platform.connectors.total == probes


# Usage


def flow(*nodes: dict, env: list[dict] | None = None, start: dict | None = None) -> dict:
    """A flow app's configuration; only what usage reads."""
    first = start or {"id": "start", "type": "start", "inputs": []}
    return {
        "schema_version": 1,
        "env": {"vars": env or [], "secrets": []},
        "graph": {"nodes": [first, *nodes, {"id": "end", "type": "end"}], "edges": []},
    }


def tool(datasource: str) -> dict:
    return {"id": "lookup", "type": "tool", "datasource": datasource, "tool": "list_schemas"}


async def test_usage(platform):
    shop = (await platform.register())["datasource_id"]
    crm = (await platform.register("crm", "http://tools.connector.test/mcp", "mcp"))[
        "datasource_id"
    ]
    picked = {"key": "source", "label": "数据源", "type": "datasource", "default": shop}
    braced = {
        "key": "pick",
        "label": "选一个",
        "type": "select",
        "default": "{" + shop.upper() + "}",
        "choices": [{"value": crm, "label": "CRM"}],
    }
    apps = {
        "app-literal": flow(tool(shop)),
        "app-default": flow(start={"id": "start", "type": "start", "inputs": [picked]}),
        "app-both": flow(tool(shop), start={"id": "start", "type": "start", "inputs": [picked]}),
        "app-env": flow(tool("{{env.src}}"), env=[{"key": "src", "value": shop.upper()}]),
        "app-ref": flow(
            tool("{{entry.pick}}"), start={"id": "entry", "type": "start", "inputs": [braced]}
        ),
        "app-agent": flow(
            {"id": "helper", "type": "agent", "tools": [{"datasource": crm, "tool": "x"}]}
        ),
        "app-draft": flow(),
        "app-old": flow(tool(shop)),
        "app-archived": flow(tool(shop)),
    }
    runs = platform.service
    for app, config in apps.items():
        await runs.flows.ensure(app, f"应用 {app}", "")
        await runs.versions.ensure(app, config)
    await runs.versions.save_draft("app-draft", flow(tool(shop)), None)
    await runs.versions.save_draft("app-old", flow(), None)
    await runs.versions.publish("app-old", "不再用 shop", None)
    await runs.versions.save_draft("app-literal", flow(tool(shop)), None)
    await runs.versions.publish("app-literal", "第二版", None)
    await runs.flows.archive("app-archived")
    await runs.flows.ensure("app-broken", "没有版本的应用", "")  # its version cannot be read

    used = (await platform.api.get(f"/datasources/{shop}/usage")).json()["apps"]
    assert used == [
        {"app": "app-both", "title": "应用 app-both", "version": 1, "via": "literal"},
        {"app": "app-default", "title": "应用 app-default", "version": 1, "via": "default_input"},
        {"app": "app-env", "title": "应用 app-env", "version": 1, "via": "literal"},
        {"app": "app-literal", "title": "应用 app-literal", "version": 2, "via": "literal"},
        {"app": "app-ref", "title": "应用 app-ref", "version": 1, "via": "default_input"},
    ]
    used = (await platform.api.get(f"/datasources/{crm}/usage")).json()["apps"]
    assert used == [{"app": "app-agent", "title": "应用 app-agent", "version": 1, "via": "literal"}]
    other = await platform.register("other", "http://copy.connector.test/mcp")
    assert (await platform.api.get(f"/datasources/{other['datasource_id']}/usage")).json() == {
        "apps": []
    }


# Registering the defaults, and the ETL tools


async def test_defaults_are_registered_once():
    store = MemoryDataSourceStore()
    defaults = [{"name": "示例库", "kind": "postgres", "connector_url": fake_connector.URL}]

    assert await register_defaults(store, defaults) == 1
    assert await register_defaults(store, defaults) == 0
    [ds] = await store.all()
    # Renamed on the page: the next start does not register it again.
    await store.update(ds.datasource_id, name="订单示例")
    assert await register_defaults(store, defaults) == 0
    written_otherwise = [{**defaults[0], "connector_url": "HTTP://connector.test:80/mcp/"}]
    assert await register_defaults(store, written_otherwise) == 0
    assert [d.name for d in await store.all()] == ["订单示例"]


async def test_demo_source_is_found_by_its_connector_after_a_rename():
    store = MemoryDataSourceStore()
    server = fake_connector.build()
    tools = McpTools(5, resolve=lambda url: server)

    async def found(settings: Settings) -> str:
        items = await demo_checks(settings, [], store, tools, None)
        [item] = [i for i in items if i["key"] == "datasource"]
        return item["detail"]

    # A tool service on the same connector, sorting first, is never the demo's source.
    await store.create("A 查询工具", "mcp", DEMO_CONNECTOR_URL)
    await store.create("零售演示库", "mysql", DEMO_CONNECTOR_URL)
    assert (await found(Settings())).startswith("数据源「零售演示库」里没有 retail 库")
    # The connector is the AEP_DEFAULT_DATASOURCES entry's when there is one.
    moved = await store.create("迁移后的演示库", "mysql", "http://mysql.connector.test/mcp")
    default = {"name": "MySQL 示例库", "kind": "mysql", "connector_url": moved.connector_url}
    detail = await found(Settings(default_datasources=[default]))
    assert detail.startswith("数据源「迁移后的演示库」")
    # A source with the demo's name wins over one on its connector.
    await store.create("MySQL 示例库", "postgres", fake_connector.URL)
    assert (await found(Settings())).startswith("数据源「MySQL 示例库」")


async def test_evals_run_on_a_data_source_never_a_tool_service(gateway):
    sources = MemoryDataSourceStore()
    settings = Settings()
    service = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(gateway.client, settings, datasources=sources),
    )
    catalog = AppCatalog(service, sources, settings)
    await sources.create("工单系统", "mcp", "http://tools.connector.test/mcp")
    with pytest.raises(RuntimeError, match="平台里没有登记数据源"):
        await catalog._datasource_id()
    shop = await sources.create("示例库", "postgres", fake_connector.URL)
    assert await catalog._datasource_id() == shop.datasource_id
    # AEP_EVAL_DATASOURCE names a default source renamed since: found by its connector.
    settings.eval_datasource = "示例库"
    settings.default_datasources = [
        {"name": "示例库", "kind": "postgres", "connector_url": "HTTP://connector.test/mcp"}
    ]
    await sources.update(shop.datasource_id, name="订单示例")
    assert await catalog._datasource_id() == shop.datasource_id
    settings.eval_datasource = "工单系统"
    with pytest.raises(RuntimeError, match="AEP_EVAL_DATASOURCE"):
        await catalog._datasource_id()


def test_etl_tool_lists_are_subsets():
    assert set(PROFILE_TOOLS) <= set(ETL_TOOLS)
    assert set(INGEST_TOOLS) <= set(ETL_TOOLS)


async def test_fake_connector_offers_exactly_the_etl_tools():
    server = fake_connector.build()
    listed = await McpTools(5, resolve=lambda url: server).list_tools(fake_connector.URL)
    assert sorted(t.name for t in listed) == sorted(ETL_TOOLS)


# The tool client


def tools_for(server=None, timeout_seconds: float = 5) -> McpTools:
    server = server or fake_connector.build()
    return McpTools(
        timeout_seconds, resolve=lambda url: server if url == fake_connector.URL else url
    )


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
    with pytest.raises(ConnectorUnavailable, match="no answer") as caught:
        await tools.call(
            fake_connector.URL, "describe_table", {"schema": "sales", "table": "orders"}
        )
    assert caught.value.reason == "timeout"
