"""The MCP client's connection problems: what kind each one is, masked addresses in its
messages, redirects, and listing many tools. No network: scripted HTTP replies."""

import errno
import json
import socket
import ssl
from collections.abc import Awaitable, Callable

import anyio
import httpcore2
import httpx2
import pytest
from mcp import types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from aep.tools.mcp import MAX_TOOL_PAGES, MAX_TOOLS, ConnectorUnavailable, McpTools, classify

Handler = Callable[[httpx2.Request], Awaitable[httpx2.Response]]


def scripted(handler: Handler, timeout_seconds: float = 2) -> McpTools:
    """Every address answered by ``handler``, as an HTTP server would."""

    def resolve(url: str):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        return streamable_http_client(url, http_client=client)

    return McpTools(timeout_seconds, resolve=resolve)


def answering(tools_list: dict) -> McpTools:
    """A server that completes the handshake and answers tools/list with ``tools_list``."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            return httpx2.Response(405)
        message = json.loads(request.content)
        if "id" not in message:  # a notification
            return httpx2.Response(202)
        result = tools_list
        if message["method"] == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "scripted", "version": "1"},
            }
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    return scripted(handler)


def connect_error(root: BaseException) -> BaseExceptionGroup:
    """What the SDK raises when the connection fails: the transport's error inside a task
    group, with the socket error underneath."""
    socket_error = OSError("All connection attempts failed")
    socket_error.__cause__ = root
    inner = httpcore2.ConnectError("All connection attempts failed")
    inner.__context__ = socket_error
    outer = httpx2.ConnectError("All connection attempts failed")
    outer.__cause__ = inner
    return ExceptionGroup("unhandled errors in a TaskGroup", [outer])


def test_classify_judges_the_whole_chain():
    refused = ConnectionRefusedError(111, "Connect call failed")
    assert classify(connect_error(socket.gaierror(-2, "Name or service not known"))) == "other"
    assert classify(connect_error(refused)) == "refused"
    both = connect_error(refused)
    both.exceptions[0].__context__ = socket.gaierror(-2, "x")
    assert classify(both) == "refused"  # a refused connection wins over an unknown host
    assert classify(connect_error(OSError(errno.ENETUNREACH, "unreachable"))) == "other"
    assert classify(connect_error(OSError(errno.EHOSTUNREACH, "no route"))) == "other"
    assert classify(connect_error(ssl.SSLError(1, "WRONG_VERSION_NUMBER"))) == "other"
    assert classify(ExceptionGroup("g", [httpx2.ConnectError("x")])) == "refused"
    assert classify(ExceptionGroup("g", [httpx2.ReadTimeout("x")])) == "timeout"
    assert classify(TimeoutError()) == "timeout"
    assert classify(MCPError(-32001, "Request timed out")) == "timeout"
    for code in (-32000, -32600, -32601, -32603, -32700):
        assert classify(ExceptionGroup("g", [MCPError(code, "x")])) == "not_mcp"
    assert classify(httpx2.RemoteProtocolError("x")) == "not_mcp"
    assert classify(json.JSONDecodeError("x", "<html>", 0)) == "not_mcp"
    assert classify(httpx2.UnsupportedProtocol("x")) == "other"
    assert classify(RuntimeError("x")) == "other"
    looped = RuntimeError("a")
    looped.__context__ = looped
    assert classify(looped) == "other"


async def html(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, text="<html>登录</html>", headers={"content-type": "text/html"})


async def not_found(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(404, text="Not Found")


async def misdirected(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(421, text="Invalid Host header")


async def refused(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("All connection attempts failed")


async def slow(request: httpx2.Request) -> httpx2.Response:
    await anyio.sleep(5)
    return httpx2.Response(200)


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (html, "not_mcp"),
        (not_found, "not_mcp"),
        (misdirected, "not_mcp"),  # the connector does not allow this host name
        (refused, "refused"),
        (slow, "timeout"),
    ],
)
async def test_connection_problems_have_a_reason_and_a_masked_address(handler, reason):
    url = "http://reader:s3cret@connector.test/mcp?token=abc&schema=sales"
    with pytest.raises(ConnectorUnavailable) as caught:
        await scripted(handler).list_tools(url, timeout_seconds=0.3)
    assert caught.value.reason == reason
    message = str(caught.value)
    assert message.startswith("http://***@connector.test/mcp?token=***&schema=sales: ")
    assert "s3cret" not in message and "abc" not in message


async def test_an_address_that_does_not_parse_quotes_no_part_of_its_password():
    # Stored before addresses were checked: the / ends the host part, so httpx reads
    # "Pa" as the port and would quote it. Nothing is connected to.
    url = "http://reporter:Pa/ss#1@127.0.0.1:1/mcp"
    with pytest.raises(ConnectorUnavailable) as caught:
        await McpTools(1).list_tools(url)
    message = str(caught.value)
    assert message == "http://***@127.0.0.1:1/mcp: the address does not parse"
    assert caught.value.reason == "other"


@pytest.mark.parametrize(
    "tools_list",
    [
        {"tools": [{"name": "a", "inputSchema": "x"}]},
        {"tools": [{"name": "a"}]},  # no inputSchema
        {"tools": [{"inputSchema": {"type": "object"}}]},  # no name
        {"tools": "a"},
    ],
)
async def test_a_reply_the_sdk_cannot_read_is_not_mcp(tools_list):
    # It answered the handshake, so the network is fine: the server is not a proper one.
    with pytest.raises(ConnectorUnavailable) as caught:
        await answering(tools_list).list_tools("http://connector.test/mcp")
    assert caught.value.reason == "not_mcp"


async def test_redirects_to_another_host_are_not_followed():
    seen: list[str] = []

    async def redirect(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.host)
        if request.url.host == "169.254.169.254":
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})
        return httpx2.Response(307, headers={"location": "http://169.254.169.254/latest/mcp"})

    with pytest.raises(ConnectorUnavailable) as caught:
        await scripted(redirect).list_tools("http://connector.test/mcp")
    assert caught.value.reason == "not_mcp"
    assert seen and set(seen) == {"connector.test"}


async def test_listing_follows_pages_up_to_the_limits():
    asked: list[int] = []

    def server(per_page: int) -> Server:
        async def on_list_tools(ctx, params):
            page = int(params.cursor) if params and params.cursor else 0
            asked.append(page)
            tools = [
                types.Tool(
                    name=f"t{page}_{i}",
                    description="说" * 3000,
                    input_schema={"type": "object"},
                    annotations=types.ToolAnnotations(read_only_hint=i % 2 == 0),
                )
                for i in range(per_page)
            ]
            return types.ListToolsResult(tools=tools, next_cursor=str(page + 1))

        return Server("paged", on_list_tools=on_list_tools)

    big = server(120)
    found = await McpTools(5, resolve=lambda url: big).list_tools("http://paged.test/mcp")
    assert len(found) == MAX_TOOLS and asked == [0, 1, 2, 3, 4]
    assert len(found[0].description) == 2000
    assert [t.read_only for t in found[:2]] == [True, False]
    asked.clear()
    small = server(1)
    found = await McpTools(5, resolve=lambda url: small).list_tools("http://paged.test/mcp")
    assert len(found) == MAX_TOOL_PAGES and asked == list(range(MAX_TOOL_PAGES))


async def test_an_empty_or_repeated_cursor_ends_the_listing():
    tool = {"name": "a", "inputSchema": {"type": "object"}}
    found = await answering({"tools": [tool], "nextCursor": ""}).list_tools("http://c.test/mcp")
    assert [t.name for t in found] == ["a"]

    asked: list[str | None] = []

    async def on_list_tools(ctx, params):
        asked.append(params.cursor if params else None)
        tools = [types.Tool(name=n, input_schema={"type": "object"}) for n in ("a", "b", "a")]
        return types.ListToolsResult(tools=tools, next_cursor="same")  # ignores the cursor

    stuck = Server("stuck", on_list_tools=on_list_tools)
    found = await McpTools(5, resolve=lambda url: stuck).list_tools("http://stuck.test/mcp")
    assert [t.name for t in found] == ["a", "b"]
    assert asked == [None, "same"]
