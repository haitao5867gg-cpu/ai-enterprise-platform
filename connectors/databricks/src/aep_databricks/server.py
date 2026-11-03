"""Official MCP SDK server, served over Streamable HTTP at /mcp."""

from collections.abc import Awaitable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.requests import Request
from starlette.responses import JSONResponse

from aep_databricks.config import Settings
from aep_databricks.executor import Executor, StatementError
from aep_databricks.reader import Reader
from aep_databricks.sql import ConnectorError

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

SchemaName = Annotated[str, Field(description="catalog.schema, e.g. main.sales")]
TableName = Annotated[str, Field(description="catalog.schema.table")]
Where = Annotated[
    str | None,
    Field(description="Optional Spark SQL boolean expression; only matching rows are read"),
]
Limit = Annotated[int, Field(strict=True, ge=1, le=1000)]


class Lookup(BaseModel):
    """Only the rows that do (``found``) or do not have a matching row in another table."""

    model_config = ConfigDict(extra="forbid")

    table: TableName
    on: Annotated[
        dict[str, str],
        Field(
            min_length=1,
            max_length=10,
            description="{column of this table: column of the other table}; nulls match nulls",
        ),
    ]
    where: Annotated[
        str | None, Field(description="Optional condition on the other table's rows")
    ] = None
    found: Annotated[
        StrictBool,
        Field(description="true: rows that have a matching row; false: rows that have none"),
    ]
    trim: Annotated[
        StrictBool,
        Field(description="Compare the on columns without leading and trailing spaces"),
    ] = False


def http_security(settings: Settings) -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.databricks_mcp_allowed_hosts,
        allowed_origins=settings.databricks_mcp_allowed_origins,
    )


def create_server(settings: Settings | None = None, executor: Executor | None = None) -> MCPServer:
    settings = settings or Settings()
    reader = Reader(settings, executor)
    server = MCPServer(
        "aep-databricks",
        version="0.1.0",
        instructions=(
            "Read-only Databricks tables in the allowed Unity Catalog catalogs. Schemas are "
            "catalog.schema, tables catalog.schema.table. No arbitrary SQL or write tools."
        ),
    )

    async def call(operation: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        try:
            return await operation
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=READ_ONLY)
    async def list_schemas() -> dict[str, Any]:
        """List schemas (catalog.schema) in the catalogs this connector may read."""
        return await call(reader.list_schemas())

    @server.tool(annotations=READ_ONLY)
    async def list_tables(schema: SchemaName) -> dict[str, Any]:
        """List tables and views in a schema (catalog.schema)."""
        return await call(reader.list_tables(schema))

    @server.tool(annotations=READ_ONLY)
    async def describe_table(schema: SchemaName, table: str) -> dict[str, Any]:
        """Return ordered columns with Databricks types, nullability, comments, and the
        primary and foreign keys registered in Unity Catalog (informational only)."""
        return await call(reader.describe_table(schema, table))

    @server.tool(annotations=READ_ONLY)
    async def sample_rows(
        schema: SchemaName,
        table: str,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 10,
        where: Where = None,
        lookup: Lookup | None = None,
    ) -> dict[str, Any]:
        """Read at most 100 rows as JSON, without an ordering guarantee. ``lookup`` keeps
        only the rows that do (or do not) have a matching row in another table."""
        found = lookup.model_dump() if lookup else None
        return await call(reader.sample_rows(schema, table, limit, where, found))

    @server.tool(annotations=READ_ONLY)
    async def read_rows(
        schema: SchemaName,
        table: str,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=5000)] = 1000,
        where: Where = None,
    ) -> dict[str, Any]:
        """Read a stable page ordered by the registered primary key, or all columns."""
        return await call(reader.read_rows(schema, table, offset, limit, where))

    @server.tool(annotations=READ_ONLY)
    async def table_stats(schema: SchemaName, table: str, where: Where = None) -> dict[str, Any]:
        """Return the exact row count (with ``where``) and, on a bounded sample, null
        fractions and distinct counts. Columns with at most 20 distinct values in the sample
        also list their value counts (``top_values``), most frequent first; values longer
        than 80 characters are shortened and marked ``truncated``."""
        return await call(reader.table_stats(schema, table, where))

    @server.tool(annotations=READ_ONLY)
    async def aggregate(
        table: TableName,
        expr: Annotated[str, Field(description="Spark SQL aggregate, e.g. sum(amount)")],
        where: Where = None,
        group_by: list[str] | None = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=10000)] = 20,
        lookup: Lookup | None = None,
    ) -> dict[str, Any]:
        """One aggregate value, or one per group (ordered by the group columns, at most
        ``limit`` groups). ``groups`` counts every group, so it can exceed the rows.
        ``lookup`` keeps only the rows that do (or do not) have a matching row in another
        table."""
        found = lookup.model_dump() if lookup else None
        return await call(reader.aggregate(table, expr, where, group_by, limit, found))

    @server.tool(annotations=READ_ONLY)
    async def duplicates(
        table: TableName, columns: list[str], where: Where = None, limit: Limit = 20
    ) -> dict[str, Any]:
        """Groups of rows that repeat the given columns, most repeated first."""
        return await call(reader.duplicates(table, columns, where, limit))

    @server.tool(annotations=READ_ONLY)
    async def compare(
        table: TableName,
        expected: TableName,
        keys: list[str],
        columns: list[str] | None = None,
        where: Where = None,
        expected_where: Where = None,
        tolerance: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        limit: Limit = 20,
    ) -> dict[str, Any]:
        """Compare two tables row by row on ``keys``. Repeated keys on either side are
        reported first and stop the comparison; otherwise counts rows missing from
        ``table``, extra in it, and different (numbers within ``tolerance`` and two nulls
        count as the same). ``where`` filters ``table`` only, ``expected_where`` only
        ``expected``."""
        return await call(
            reader.compare(table, expected, keys, columns, where, expected_where, tolerance, limit)
        )

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        # Without ?probe=1 nothing reaches Databricks: the container check runs every few
        # seconds and a statement would keep a warehouse or cluster from ever stopping.
        missing = settings.missing()
        if request.query_params.get("probe") != "1":
            return JSONResponse({"status": "ok", "configured": not missing, "missing": missing})
        if missing:
            return JSONResponse(
                {"status": "error", "problem": "缺少设置：" + "、".join(missing)}, status_code=503
            )
        try:
            await reader.run("SELECT 1")
        except StatementError:
            return JSONResponse(
                {"status": "error", "problem": "Databricks 执行 SELECT 1 失败"}, status_code=503
            )
        except ConnectorError as exc:
            return JSONResponse({"status": "error", "problem": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok", "configured": True, "missing": []})

    return server


def main() -> None:
    settings = Settings()
    create_server(settings).run(
        transport="streamable-http",
        host=settings.databricks_mcp_host,
        port=settings.databricks_mcp_port,
        stateless_http=True,
        json_response=True,
        transport_security=http_security(settings),
    )


if __name__ == "__main__":
    main()
