"""Acceptance check against a real workspace over MCP HTTP (not run in CI).

Start the connector with the AEP_DATABRICKS_READ_* settings, then:

    aep-databricks-smoke --table main.sales.orders --key order_id

It calls every tool once on that table (compare against itself must find no
differences) and prints counts only, never rows.
"""

import argparse
import asyncio
import json
from typing import Any

from mcp import Client

from aep_databricks.config import Settings

TOOLS = {
    "list_schemas",
    "list_tables",
    "describe_table",
    "sample_rows",
    "read_rows",
    "table_stats",
    "aggregate",
    "duplicates",
    "compare",
}


async def check(url: str, table: str, key: list[str]) -> dict[str, Any]:
    catalog, schema, name = table.split(".")
    async with Client(url, read_timeout_seconds=300) as client:
        available = await client.list_tools()
        assert {tool.name for tool in available.tools} == TOOLS
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in available.tools)

        async def call(tool: str, **arguments: Any) -> dict[str, Any]:
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, (tool, result.content)
            assert isinstance(result.structured_content, dict), tool
            return result.structured_content

        two = f"{catalog}.{schema}"
        assert two in (await call("list_schemas"))["schemas"]
        assert name in (await call("list_tables", schema=two))["tables"]
        description = await call("describe_table", schema=two, table=name)
        rows = await call("sample_rows", schema=two, table=name, limit=5)
        page = await call("read_rows", schema=two, table=name, limit=5)
        stats = await call("table_stats", schema=two, table=name)
        total = await call("aggregate", table=table, expr="count(*)")
        assert total["rows"][0]["value"] == stats["estimated_row_count"]
        repeated = await call("duplicates", table=table, columns=key)
        same = await call("compare", table=table, expected=table, keys=key)
        if not repeated["groups"]:
            assert (same["missing"], same["extra"], same["different"]) == (0, 0, 0), same
        refused = await client.call_tool(
            "sample_rows", {"schema": two, "table": name, "where": "1 = 1; SELECT 1"}
        )
        assert refused.is_error
    return {
        "status": "ok",
        "columns": len(description["columns"]),
        "sample_rows": len(rows["rows"]),
        "page_rows": len(page["rows"]),
        "row_count": stats["estimated_row_count"],
        "duplicate_groups": repeated["groups"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=f"http://127.0.0.1:{Settings().databricks_mcp_port}/mcp")
    parser.add_argument("--table", required=True, help="catalog.schema.table")
    parser.add_argument("--key", action="append", required=True, help="key column (repeatable)")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check(args.url, args.table, args.key)), ensure_ascii=False))


if __name__ == "__main__":
    main()
