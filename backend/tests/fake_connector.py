"""In-process stand-ins for data source connectors.

``build``: same tools and result shapes as connectors/postgres (issue #6), serving a
small shop database (customers and orders) with some dirty rows. ``build_databricks``:
the 6 tools of connectors/databricks over two tables of a Databricks catalog.
"""

import re

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
    def read_rows(schema: str, table: str, offset: int = 0, limit: int = 1000) -> dict:
        """A page of rows in primary key order."""
        t = _table(schema, table)
        end = offset + limit
        return {
            "schema": schema,
            "table": table,
            "columns": [c["name"] for c in t["columns"]],
            "rows": t["rows"][offset:end],
            "next_offset": end if end < len(t["rows"]) else None,
        }

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


# A Databricks data source (connectors/databricks): schemas are catalog.schema, tables
# carry comments, and the row tools take a ``where`` (here only ``column = 'value'``).

DATABRICKS_URL = "http://databricks.connector.test/mcp"
DATABRICKS_TABLES = {
    ("prev.sales", "forecast"): {
        "comment": "上一期的需求预测",
        "columns": [
            _col("sku", "string", nullable=False) | {"comment": "物料号"},
            _col("month", "string") | {"comment": None},
            _col("Type", "string") | {"comment": "Vol. 是数量，Amt. 是金额"},
            _col("value", "decimal(18,2)") | {"comment": None},
        ],
        "foreign_keys": [],
        "rows": [
            {"sku": "A1", "month": "2026-10", "Type": "Vol.", "value": 10},
            {"sku": "A1", "month": "2026-10", "Type": "Amt.", "value": 1200.5},
            {"sku": "B2", "month": "2026-11", "Type": "Vol.", "value": 4},
        ],
    },
    ("prev.sales", "sku"): {
        "comment": None,
        "columns": [_col("sku", "string", nullable=False) | {"comment": None}],
        "foreign_keys": [],
        "rows": [{"sku": "A1"}, {"sku": "B2"}],
    },
}
_EQUALS = re.compile(r"^\s*`?(\w+)`?\s*=\s*'([^']*)'\s*$")


def build_databricks(calls: list[tuple[str, dict]] | None = None) -> MCPServer:
    """The fake Databricks connector; ``calls`` gets each tool call (name, arguments)."""
    server = MCPServer("fake-databricks")
    seen = calls if calls is not None else []

    def find(schema: str, name: str) -> dict:
        try:
            return DATABRICKS_TABLES[(schema.lower(), name.lower())]
        except KeyError:
            raise ToolError(f"表 {schema}.{name} 不存在，或者没有读权限") from None

    def rows(schema: str, name: str, where: str | None) -> list[dict]:
        found = find(schema, name)["rows"]
        if where is None:
            return found
        match = _EQUALS.match(where)
        if match is None:
            raise ToolError("读取条件写法不对：这个假连接器只认 column = 'value'")
        column, value = match.groups()
        return [r for r in found if str(r.get(column)) == value]

    @server.tool()
    def list_schemas() -> dict:
        """catalog.schema names."""
        seen.append(("list_schemas", {}))
        return {"schemas": sorted({schema for schema, _ in DATABRICKS_TABLES})}

    @server.tool()
    def list_tables(schema: str) -> dict:
        """Tables and views of a catalog.schema."""
        seen.append(("list_tables", {"schema": schema}))
        names = sorted(t for s, t in DATABRICKS_TABLES if s == schema.lower())
        return {"schema": schema, "tables": names}

    @server.tool()
    def describe_table(schema: str, table: str) -> dict:
        """Columns with comments, and the table's comment."""
        seen.append(("describe_table", {"schema": schema, "table": table}))
        t = find(schema, table)
        return {
            "schema": schema,
            "table": table,
            "comment": t["comment"],
            "columns": t["columns"],
            "foreign_keys": t["foreign_keys"],
        }

    @server.tool()
    def sample_rows(schema: str, table: str, limit: int = 10, where: str | None = None) -> dict:
        """At most 100 rows, those ``where`` keeps."""
        seen.append(("sample_rows", {"schema": schema, "table": table, "where": where}))
        return {"schema": schema, "table": table, "rows": rows(schema, table, where)[:limit]}

    @server.tool()
    def read_rows(
        schema: str, table: str, offset: int = 0, limit: int = 1000, where: str | None = None
    ) -> dict:
        """A page of rows."""
        seen.append(("read_rows", {"schema": schema, "table": table, "where": where}))
        found = rows(schema, table, where)
        end = offset + limit
        return {
            "schema": schema,
            "table": table,
            "columns": [c["name"] for c in find(schema, table)["columns"]],
            "rows": found[offset:end],
            "next_offset": end if end < len(found) else None,
        }

    @server.tool()
    def table_stats(schema: str, table: str, where: str | None = None) -> dict:
        """Exact row count with ``where``; per column stats of the rows it keeps."""
        seen.append(("table_stats", {"schema": schema, "table": table, "where": where}))
        found = rows(schema, table, where)
        columns = []
        for col in find(schema, table)["columns"]:
            values = [r[col["name"]] for r in found]
            present = [v for v in values if v is not None]
            stats = {
                "name": col["name"],
                "null_fraction": (len(values) - len(present)) / len(values) if values else 0,
                "distinct_count": len(set(present)),
            }
            if col["data_type"] == "string":
                stats["top_values"] = [
                    {"value": v, "count": present.count(v)} for v in sorted(set(present))
                ]
            columns.append(stats)
        return {
            "schema": schema,
            "table": table,
            "estimated_row_count": len(found),
            "sample_limit": 1000,
            "sample_row_count": len(found),
            "columns": columns,
        }

    return server
