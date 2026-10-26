"""An in-process stand-in for a relational database connector.

Same tools and result shapes as connectors/postgres (issue #6), serving a small
shop database (customers and orders) with some dirty rows.
"""

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

URL = "http://connector.test/mcp"


def _col(name: str, data_type: str, nullable: bool = True, primary_key: bool = False) -> dict:
    return {"name": name, "data_type": data_type, "nullable": nullable, "primary_key": primary_key}


TABLES = {
    ("sales", "customers"): {
        "columns": [
            _col("customer_id", "bigint", nullable=False, primary_key=True),
            _col("email", "text"),
            _col("city", "text"),
        ],
        "foreign_keys": [],
        "rows": [
            {"customer_id": 1, "email": "a@example.com", "city": "上海"},
            {"customer_id": 2, "email": None, "city": " 上海 "},
            {"customer_id": 3, "email": "A@example.com", "city": "北京"},
        ],
    },
    ("sales", "orders"): {
        "columns": [
            _col("order_id", "bigint", nullable=False, primary_key=True),
            _col("customer_id", "bigint", nullable=False),
            _col("quantity", "integer"),
            _col("status", "text"),
            _col("ordered_at", "timestamp with time zone", nullable=False),
        ],
        "foreign_keys": [
            {
                "name": "orders_customer_id_fkey",
                "columns": ["customer_id"],
                "referenced_schema": "sales",
                "referenced_table": "customers",
                "referenced_columns": ["customer_id"],
            }
        ],
        "rows": [
            {
                "order_id": 10,
                "customer_id": 1,
                "quantity": 2,
                "status": "paid",
                "ordered_at": "2026-10-01T00:00:00Z",
            },
            {
                "order_id": 11,
                "customer_id": 2,
                "quantity": -1,
                "status": "Paid",
                "ordered_at": "2026-10-02T00:00:00Z",
            },
            {
                "order_id": 12,
                "customer_id": 3,
                "quantity": None,
                "status": None,
                "ordered_at": "2026-10-03T00:00:00Z",
            },
        ],
    },
}


def _table(schema: str, table: str) -> dict:
    try:
        return TABLES[(schema, table)]
    except KeyError:
        raise ToolError("Table does not exist or SELECT access is not granted") from None


def build(delay_seconds: float = 0) -> MCPServer:
    server = MCPServer("fake-postgres")

    @server.tool()
    def list_schemas() -> dict:
        """List accessible, non-system schemas."""
        return {"schemas": sorted({schema for schema, _ in TABLES})}

    @server.tool()
    def list_tables(schema: str) -> dict:
        """List readable tables in a schema."""
        return {"schema": schema, "tables": sorted(t for s, t in TABLES if s == schema)}

    @server.tool()
    async def describe_table(schema: str, table: str) -> dict:
        """Columns, types, nullability, primary-key flags and foreign keys."""
        await anyio.sleep(delay_seconds)
        t = _table(schema, table)
        return {
            "schema": schema,
            "table": table,
            "columns": t["columns"],
            "foreign_keys": t["foreign_keys"],
        }

    @server.tool()
    def sample_rows(schema: str, table: str, limit: int = 10) -> dict:
        """At most 100 rows as JSON."""
        return {"schema": schema, "table": table, "rows": _table(schema, table)["rows"][:limit]}

    @server.tool()
    def table_stats(schema: str, table: str) -> dict:
        """Row estimate, per column the null fraction and distinct count of a sample, and
        value counts of text columns."""
        t = _table(schema, table)
        rows = t["rows"]
        columns = []
        for col in t["columns"]:
            values = [r[col["name"]] for r in rows]
            stats = {
                "name": col["name"],
                "null_fraction": sum(v is None for v in values) / len(values),
                "distinct_count": len({v for v in values if v is not None}),
            }
            if col["data_type"] == "text":
                present = [v for v in values if v is not None]
                stats["top_values"] = [
                    {"value": v, "count": present.count(v)} for v in sorted(set(present))
                ]
            columns.append(stats)
        return {
            "schema": schema,
            "table": table,
            "estimated_row_count": len(rows),
            "sample_limit": 1000,
            "sample_row_count": len(rows),
            "columns": columns,
        }

    return server
