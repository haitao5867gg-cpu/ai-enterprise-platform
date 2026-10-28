"""Stateless MCP Streamable HTTP with one tool, ``run_python``."""

from typing import Annotated, Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from aep_sandbox.config import Settings
from aep_sandbox.runner import Runner

MAX_CODE = 20000


def build(settings: Settings | None = None) -> MCPServer:
    settings = settings or Settings()
    runner = Runner(settings)
    server = MCPServer("aep-sandbox", version="0.1.0", instructions="Runs code-node Python.")

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        )
    )
    async def run_python(
        code: Annotated[str, Field(min_length=1, max_length=MAX_CODE)],
        inputs: dict[str, Any],
        timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 10,
    ) -> dict[str, Any]:
        """Run ``main(inputs)`` from the code and return what it returns.

        The answer is {"status": "ok", "result", "logs"}, {"status": "error", "error",
        "logs"} or {"status": "timeout", "error", "logs"}; logs is the tail of what the
        code printed. Standard library, pandas and numpy; no network, no credentials.
        """
        return await runner.run(code, inputs, timeout_seconds)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return server


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings()
    return build(settings).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.sandbox_mcp_allowed_hosts,
            allowed_origins=settings.sandbox_mcp_allowed_origins,
        ),
    )


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings), host=settings.sandbox_mcp_host, port=settings.sandbox_mcp_port
    )


if __name__ == "__main__":
    main()
