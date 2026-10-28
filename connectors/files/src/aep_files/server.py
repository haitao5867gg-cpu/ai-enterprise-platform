"""Stateless MCP Streamable HTTP, routed by UUID dataset rather than SQL input."""

from typing import Annotated, Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from aep_files.config import Settings
from aep_files.database import Database

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings()
    database = Database(settings)
    server = MCPServer("aep-files", version="0.1.0", instructions="Read-only file discovery.")

    async def call(ctx: Context, operation: str, *args: Any) -> dict[str, Any]:
        request = ctx.request_context.request
        dataset_id = str(request.path_params["dataset_id"])
        try:
            return await run_in_threadpool(getattr(database, operation), dataset_id, *args)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=READ_ONLY)
    async def list_schemas(ctx: Context) -> dict[str, Any]:
        """List the uploaded dataset's schema."""
        return await call(ctx, "list_schemas")

    @server.tool(annotations=READ_ONLY)
    async def list_tables(schema: str, ctx: Context) -> dict[str, Any]:
        """List tables in the dataset manifest."""
        return await call(ctx, "list_tables", schema)

    @server.tool(annotations=READ_ONLY)
    async def describe_table(schema: str, table: str, ctx: Context) -> dict[str, Any]:
        """Return ordered columns, types, nullability, primary-key flags and foreign keys."""
        return await call(ctx, "describe_table", schema, table)

    @server.tool(annotations=READ_ONLY)
    async def sample_rows(
        schema: str,
        table: str,
        ctx: Context,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 10,
    ) -> dict[str, Any]:
        """Read at most 100 rows as JSON, without an ordering guarantee."""
        return await call(ctx, "sample_rows", schema, table, limit)

    @server.tool(annotations=READ_ONLY)
    async def table_stats(schema: str, table: str, ctx: Context) -> dict[str, Any]:
        """Return row counts and bounded-sample null fractions/distinct counts.

        Columns with 1-20 distinct sample values get top_values, most frequent first;
        values longer than 80 characters are shortened and marked truncated.
        """
        return await call(ctx, "table_stats", schema, table)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return server.streamable_http_app(
        streamable_http_path="/datasets/{dataset_id:uuid}/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.files_mcp_allowed_hosts,
            allowed_origins=settings.files_mcp_allowed_origins,
        ),
    )


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.files_mcp_host, port=settings.files_mcp_port)


if __name__ == "__main__":
    main()
