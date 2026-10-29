"""Executable acceptance check against the seeded source over real MCP HTTP."""

import argparse
import asyncio
import json
from typing import Any

from mcp import Client

from aep_postgres.config import Settings

TOOLS = {"list_schemas", "list_tables", "describe_table", "sample_rows", "read_rows", "table_stats"}


async def check(url: str) -> dict[str, Any]:
    async with Client(url, read_timeout_seconds=15) as client:
        available = await client.list_tools()
        assert {tool.name for tool in available.tools} == TOOLS
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in available.tools)

        async def call(name: str, **arguments: Any) -> dict[str, Any]:
            result = await client.call_tool(name, arguments)
            assert not result.is_error, (name, result.content)
            assert isinstance(result.structured_content, dict), name
            return result.structured_content

        assert "sales" in (await call("list_schemas"))["schemas"]
        tables = await call("list_tables", schema="sales")
        assert tables["tables"] == ["customers", "orders", "products"]
        description = await call("describe_table", schema="sales", table="orders")
        assert {c["name"] for c in description["columns"] if c["primary_key"]} == {
            "tenant_id",
            "order_id",
        }
        assert len(description["foreign_keys"]) == 2
        rows = await call("sample_rows", schema="sales", table="orders", limit=100)
        assert len(rows["rows"]) == 100
        stats = await call("table_stats", schema="sales", table="orders")
        assert stats["estimated_row_count"] == 5000
        assert 0 < stats["sample_row_count"] <= stats["sample_limit"]
        assert {c["name"] for c in stats["columns"]} == {c["name"] for c in description["columns"]}
        assert any((c["null_fraction"] or 0) > 0 for c in stats["columns"])
        invalid = await client.call_tool(
            "sample_rows", {"schema": "sales", "table": "orders", "limit": 101}
        )
        assert invalid.is_error
    return {
        "status": "ok",
        "transport": "streamable-http",
        "tools": sorted(TOOLS),
        "order_estimate": stats["estimated_row_count"],
        "sample_rows": len(rows["rows"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=f"http://127.0.0.1:{Settings().postgres_mcp_port}/mcp")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check(args.url)), ensure_ascii=False))


if __name__ == "__main__":
    main()
