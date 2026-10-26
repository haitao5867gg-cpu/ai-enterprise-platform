"""Calls tools on MCP servers (data source connectors and others).

Every call opens a short-lived session to the server, so nodes need no
connection management and a restarted connector is picked up on the next call.
Connectors speak Streamable HTTP; tests pass an in-process ``MCPServer`` instead
of a URL through ``resolve``.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from mcp import Client


class ToolError(Exception):
    """The server ran the tool and reported an error."""


class ConnectorUnavailable(ToolError):
    """The server could not be reached or did not answer in time."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]


def _leaf(exc: BaseException) -> BaseException:
    """The underlying error inside the SDK's task-group wrappers."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


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

    async def list_tools(self, url: str) -> list[ToolInfo]:
        async def run(client: Client) -> list[ToolInfo]:
            listed = await client.list_tools()
            return [ToolInfo(t.name, t.description or "", t.input_schema) for t in listed.tools]

        return await self._with_client(url, run)

    async def call(self, url: str, name: str, arguments: dict[str, Any] | None = None) -> Any:
        async def run(client: Client) -> Any:
            result = await client.call_tool(name, arguments or {})
            if result.is_error:
                raise ToolError(f"{name}: {_decode(result)}")
            return _decode(result)

        return await self._with_client(url, run)

    async def _with_client(self, url: str, run: Callable[[Client], Any]) -> Any:
        try:
            with anyio.fail_after(self._timeout):
                async with Client(self._resolve(url)) as client:
                    return await run(client)
        except TimeoutError as exc:
            raise ConnectorUnavailable(f"{url}: no answer within {self._timeout:g}s") from exc
        except ToolError:
            raise
        except Exception as exc:
            leaf = _leaf(exc)
            if isinstance(leaf, ToolError):
                raise leaf from None
            raise ConnectorUnavailable(f"{url}: {leaf}") from exc
