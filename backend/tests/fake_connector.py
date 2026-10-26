"""An in-process stand-in for a relational database connector (the tools of issue #6).

Serves a small shop database: customers and orders, with some dirty rows.
"""

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

URL = "http://connector.test/mcp"

TABLES = {
    ("public", "customers"): {
        "columns": [
            {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
            {"name": "email", "type": "text", "nullable": True, "primary_key": False},
            {"name": "country", "type": "text", "nullable": True, "primary_key": False},
        ],
        "foreign_keys": [],
        "rows": [
            {"id": 1, "email": "a@example.com", "country": "CN"},
            {"id": 2, "email": None, "country": "cn"},
            {"id": 3, "email": "c@example.com", "country": "US"},
        ],
    },
    ("public", "orders"): {
        "columns": [
            {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
            {"name": "customer_id", "type": "integer", "nullable": False, "primary_key": False},
            {"name": "amount", "type": "numeric", "nullable": True, "primary_key": False},
            {"name": "updated_at", "type": "timestamptz", "nullable": False, "primary_key": False},
        ],
        "foreign_keys": [
            {"column": "customer_id", "references": "public.customers.id"},
        ],
        "rows": [
            {"id": 10, "customer_id": 1, "amount": 99.5, "updated_at": "2026-10-01T00:00:00Z"},
            {"id": 11, "customer_id": 2, "amount": -1, "updated_at": "2026-10-02T00:00:00Z"},
            {"id": 12, "customer_id": 3, "amount": None, "updated_at": "2026-10-03T00:00:00Z"},
        ],
    },
}


def _table(schema: str, table: str) -> dict:
    try:
        return TABLES[(schema, table)]
    except KeyError:
        raise ToolError(f"table {schema}.{table} does not exist") from None


def build(delay_seconds: float = 0) -> MCPServer:
    server = MCPServer("fake-postgres")

    @server.tool()
    def list_schemas() -> list[str]:
        """Schemas in the database."""
        return sorted({schema for schema, _ in TABLES})

    @server.tool()
    def list_tables(schema: str) -> list[str]:
        """Tables in a schema."""
        return sorted(t for s, t in TABLES if s == schema)

    @server.tool()
    async def describe_table(schema: str, table: str) -> dict:
        """Columns, primary key and foreign keys of a table."""
        await anyio.sleep(delay_seconds)
        t = _table(schema, table)
        return {
            "schema": schema,
            "table": table,
            "columns": t["columns"],
            "foreign_keys": t["foreign_keys"],
        }

    @server.tool()
    def sample_rows(schema: str, table: str, limit: int = 20) -> list[dict]:
        """Up to ``limit`` (at most 100) rows of a table."""
        return _table(schema, table)["rows"][: min(limit, 100)]

    @server.tool()
    def table_stats(schema: str, table: str) -> dict:
        """Row count estimate, and per column the null rate and distinct count."""
        rows = _table(schema, table)["rows"]
        columns = {}
        for col in _table(schema, table)["columns"]:
            values = [r[col["name"]] for r in rows]
            columns[col["name"]] = {
                "null_rate": sum(v is None for v in values) / len(values),
                "distinct": len({v for v in values if v is not None}),
            }
        return {"row_count": len(rows), "columns": columns}

    return server
