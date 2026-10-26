"""Official MCP SDK server, served over Streamable HTTP at /mcp."""

from collections.abc import Awaitable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from aep_postgres.config import Settings
from aep_postgres.database import Database

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def http_security(settings: Settings) -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.postgres_mcp_allowed_hosts,
        allowed_origins=settings.postgres_mcp_allowed_origins,
    )


def create_server(settings: Settings | None = None) -> MCPServer:
    database = Database(settings or Settings())
    server = MCPServer(
        "aep-postgres",
        version="0.1.0",
        instructions="Read-only PostgreSQL discovery. No arbitrary SQL or write tools are exposed.",
    )

    async def call(operation: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        try:
            return await operation
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=READ_ONLY)
    async def list_schemas() -> dict[str, Any]:
        """List accessible, non-system schemas in the configured source database."""
        return await call(database.list_schemas())

    @server.tool(annotations=READ_ONLY)
    async def list_tables(schema: str) -> dict[str, Any]:
        """List readable base/partitioned tables in a schema; views are excluded."""
        return await call(database.list_tables(schema))

    @server.tool(annotations=READ_ONLY)
    async def describe_table(schema: str, table: str) -> dict[str, Any]:
        """Return ordered columns, types, nullability, primary-key flags and foreign keys."""
        return await call(database.describe_table(schema, table))

    @server.tool(annotations=READ_ONLY)
    async def sample_rows(
        schema: str,
        table: str,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 10,
    ) -> dict[str, Any]:
        """Read at most 100 rows as JSON, without an ordering guarantee."""
        return await call(database.sample_rows(schema, table, limit))

    @server.tool(annotations=READ_ONLY)
    async def table_stats(schema: str, table: str) -> dict[str, Any]:
        """Return a planner row estimate and bounded-sample null fractions/distinct counts.

        Columns with at most 20 distinct values in the sample also list their value counts
        (``top_values``), most frequent first.
        """
        return await call(database.table_stats(schema, table))

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        try:
            async with database.connect():
                pass
        except ValueError:
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return server


def main() -> None:
    settings = Settings()
    create_server(settings).run(
        transport="streamable-http",
        host=settings.postgres_mcp_host,
        port=settings.postgres_mcp_port,
        stateless_http=True,
        json_response=True,
        transport_security=http_security(settings),
    )


if __name__ == "__main__":
    main()
