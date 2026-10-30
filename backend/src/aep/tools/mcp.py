"""Calls tools on MCP servers (data source connectors and others).

Every call opens a short-lived session to the server, so nodes need no
connection management and a restarted connector is picked up on the next call.
Connectors speak Streamable HTTP; tests pass an in-process ``MCPServer`` instead
of a URL through ``resolve``. Errors name the server by its masked address
(``aep.connector_urls.mask_url``), as they reach run records, logs and traces.
"""

import errno
import json
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import anyio
import httpx2
import pydantic
from mcp import Client
from mcp.shared.exceptions import MCPError

from aep.connector_urls import mask_url

# What kind of connection problem a ``ConnectorUnavailable`` is; the platform says only
# this to people (docs/api/datasources.md, 问题分类), the error itself goes to the log.
ConnectionProblem = Literal["timeout", "refused", "not_mcp", "other"]
# JSON-RPC error the SDK reports when a request timed out.
REQUEST_TIMEOUT = -32001
# Listing a server's tools reads at most this many pages and keeps this many tools.
MAX_TOOL_PAGES = 10
MAX_TOOLS = 500
MAX_DESCRIPTION = 2000


class ToolError(Exception):
    """The server ran the tool and reported an error."""


class ConnectorUnavailable(ToolError):
    """The server could not be reached or did not answer in time; ``reason`` says which
    kind of problem it was."""

    def __init__(self, message: str, reason: ConnectionProblem = "other") -> None:
        super().__init__(message)
        self.reason: ConnectionProblem = reason


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    # What the server says about the tool (annotations.readOnlyHint); not checked.
    read_only: bool = False


def _leaf(exc: BaseException) -> BaseException:
    """The underlying error inside the SDK's task-group wrappers."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _chain(exc: BaseException) -> list[BaseException]:
    """The error, the members of exception groups and every cause and context, once each."""
    found: list[BaseException] = []
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        found.append(e)
        if isinstance(e, BaseExceptionGroup):
            todo.extend(e.exceptions)
        todo.extend((e.__cause__, e.__context__))
    return found


def classify(exc: BaseException) -> ConnectionProblem:
    """Which kind of connection problem an error is, judged by everything in its chain:
    the most telling error wins (a refused connection over an unreachable network, for
    example), not the outermost one."""
    chain = _chain(exc)

    def has(*types: type[BaseException]) -> bool:
        return any(isinstance(e, types) for e in chain)

    if has(TimeoutError, httpx2.TimeoutException) or any(
        isinstance(e, MCPError) and e.code == REQUEST_TIMEOUT for e in chain
    ):
        return "timeout"
    if has(ConnectionRefusedError):
        return "refused"
    unreachable = (errno.ENETUNREACH, errno.EHOSTUNREACH)
    if has(socket.gaierror, ssl.SSLError, httpx2.UnsupportedProtocol) or any(
        isinstance(e, OSError) and e.errno in unreachable for e in chain
    ):
        return "other"
    # It answered, but not as an MCP server does: an error status, HTML, or a reply the
    # SDK cannot read (a tool without a name, an input schema that is not an object).
    answered = (httpx2.RemoteProtocolError, httpx2.ReadError, httpx2.DecodingError)
    if has(MCPError, *answered, pydantic.ValidationError, json.JSONDecodeError):
        return "not_mcp"
    if has(httpx2.ConnectError):
        return "refused"
    return "other"


def _tool_info(tool: Any) -> ToolInfo:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    read_only = bool(tool.annotations and tool.annotations.read_only_hint)
    return ToolInfo(tool.name, (tool.description or "")[:MAX_DESCRIPTION], schema, read_only)


def _decode(result: Any) -> Any:
    """A tool result as plain data: structured content if any, else the text parts as JSON."""
    structured = result.structured_content
    if structured is not None:
        # Tools returning a non-object value get it wrapped as {"result": value}.
        return structured["result"] if set(structured) == {"result"} else structured
    values = []
    for part in result.content:
        text = getattr(part, "text", None)
        if text is None:
            continue
        try:
            values.append(json.loads(text))
        except ValueError:
            values.append(text)
    return values[0] if len(values) == 1 else values


class McpTools:
    def __init__(
        self, timeout_seconds: float = 30.0, resolve: Callable[[str], Any] | None = None
    ) -> None:
        self._timeout = timeout_seconds
        self._resolve = resolve or (lambda url: url)

    async def list_tools(self, url: str, timeout_seconds: float | None = None) -> list[ToolInfo]:
        """The server's tools, following pages up to ``MAX_TOOL_PAGES``, at most ``MAX_TOOLS``,
        each name once."""

        async def run(client: Client) -> list[ToolInfo]:
            found: dict[str, ToolInfo] = {}
            cursor: str | None = None
            asked: set[str] = set()
            for _ in range(MAX_TOOL_PAGES):
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:  # a name listed twice keeps its first tool
                    found.setdefault(tool.name, _tool_info(tool))
                cursor = page.next_cursor
                # An empty cursor, or one asked for before, would only repeat a page.
                if not cursor or cursor in asked or len(found) >= MAX_TOOLS:
                    break
                asked.add(cursor)
            return list(found.values())[:MAX_TOOLS]

        return await self._with_client(url, run, timeout_seconds)

    async def call(
        self,
        url: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        async def run(client: Client) -> Any:
            result = await client.call_tool(name, arguments or {})
            if result.is_error:
                raise ToolError(f"{name}: {_decode(result)}")
            return _decode(result)

        return await self._with_client(url, run, timeout_seconds)

    async def _with_client(
        self, url: str, run: Callable[[Client], Any], timeout_seconds: float | None = None
    ) -> Any:
        timeout = timeout_seconds or self._timeout
        try:
            with anyio.fail_after(timeout):
                async with Client(self._resolve(url)) as client:
                    return await run(client)
        except TimeoutError as exc:
            message = f"{mask_url(url)}: no answer within {timeout:g}s"
            raise ConnectorUnavailable(message, "timeout") from exc
        except ToolError:
            raise
        except Exception as exc:
            leaf = _leaf(exc)
            if isinstance(leaf, ToolError):
                raise leaf from None
            # An address that does not parse may quote part of a password in the error.
            unparsable = isinstance(leaf, (ValueError, httpx2.InvalidURL))
            detail = "the address does not parse" if unparsable else str(leaf)
            message = f"{mask_url(url)}: {detail.replace(url, mask_url(url))}"
            raise ConnectorUnavailable(message, classify(exc)) from exc
