"""Call all six tools against an uploaded dataset over real MCP HTTP."""

import argparse
import asyncio
import json
from typing import Any

from mcp import Client

TOOLS = {"list_schemas", "list_tables", "describe_table", "sample_rows", "read_rows", "table_stats"}


async def check(url: str) -> dict[str, Any]:
    async with Client(url, read_timeout_seconds=15) as client:
        available = (await client.list_tools()).tools
        assert {t.name for t in available} == TOOLS
        assert all(t.annotations and t.annotations.read_only_hint for t in available)

        async def call(name: str, **args: Any) -> dict[str, Any]:
            result = await client.call_tool(name, args)
            assert not result.is_error, (name, result.content)
            assert isinstance(result.structured_content, dict)
            return result.structured_content

        [schema] = (await call("list_schemas"))["schemas"]
        tables = (await call("list_tables", schema=schema))["tables"]
        assert tables
        for table in tables:
            columns = (await call("describe_table", schema=schema, table=table))["columns"]
            rows = (await call("sample_rows", schema=schema, table=table, limit=5))["rows"]
            page = await call("read_rows", schema=schema, table=table, limit=5)
            stats = await call("table_stats", schema=schema, table=table)
            assert page["columns"] == [c["name"] for c in columns]
            assert len(page["rows"]) == min(5, stats["estimated_row_count"])
            assert page["next_offset"] == (5 if stats["estimated_row_count"] > 5 else None)
            assert stats["sample_row_count"] <= stats["sample_limit"]
            assert stats["estimated_row_count"] >= len(rows)
            assert {c["name"] for c in columns} == {c["name"] for c in stats["columns"]}
    return {
        "status": "ok",
        "transport": "streamable-http",
        "tools": sorted(TOOLS),
        "schema": schema,
        "tables": tables,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="/datasets/<UUID>/mcp endpoint")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check(args.url)), ensure_ascii=False))


if __name__ == "__main__":
    main()
