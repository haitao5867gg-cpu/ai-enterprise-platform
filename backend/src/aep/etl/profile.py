"""Profile source tables through a data source's connector (read-only MCP tools)."""

import asyncio
import json
from typing import Any

from aep.datasources import DataSource
from aep.tools.mcp import McpTools

MAX_TABLES = 20
SAMPLE_ROWS = 5
MAX_VALUE_CHARS = 80


async def profile_tables(
    tools: McpTools, source: DataSource, wanted: list[str] | None = None
) -> list[dict[str, Any]]:
    """Structure, stats and a few sample rows of each table (``wanted``: schema.table names)."""
    url = source.connector_url
    if wanted:
        names = [tuple(name.split(".", 1)) for name in wanted]
    else:
        names = []
        for schema in (await tools.call(url, "list_schemas"))["schemas"]:
            listed = await tools.call(url, "list_tables", {"schema": schema})
            names += [(schema, table) for table in listed["tables"]]
    if not names:
        raise ValueError(f"数据源 {source.name} 里没有可读的表")
    if len(names) > MAX_TABLES:
        raise ValueError(f"数据源里有 {len(names)} 张表，一次最多 {MAX_TABLES} 张，请指定 tables")

    async def one(schema: str, table: str) -> dict[str, Any]:
        args = {"schema": schema, "table": table}
        described, stats, sample = await asyncio.gather(
            tools.call(url, "describe_table", args),
            tools.call(url, "table_stats", args),
            tools.call(url, "sample_rows", {**args, "limit": SAMPLE_ROWS}),
        )
        return {
            "name": f"{schema}.{table}",
            "columns": described["columns"],
            "foreign_keys": described["foreign_keys"],
            "estimated_row_count": stats.get("estimated_row_count"),
            "column_stats": stats["columns"],
            "sample": sample["rows"],
        }

    return list(await asyncio.gather(*(one(schema, table) for schema, table in names)))


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "…"
    return value


def describe_for_model(tables: list[dict[str, Any]]) -> str:
    """A compact text view of the profile for the planning prompt."""
    parts = []
    for t in tables:
        stats = {c["name"]: c for c in t["column_stats"]}
        lines = [f"表 {t['name']}（约 {t['estimated_row_count']} 行）"]
        for col in t["columns"]:
            s = stats.get(col["name"], {})
            flags = [col["data_type"]]
            if col.get("primary_key"):
                flags.append("主键")
            if not col.get("nullable", True):
                flags.append("非空")
            if s:
                flags.append(f"空值率 {s.get('null_fraction') or 0:.0%}")
                flags.append(f"不同值 {s.get('distinct_count')}")
            lines.append(f"  - {col['name']}：{'，'.join(flags)}")
            if s.get("top_values"):
                values = "，".join(
                    f"{json.dumps(v['value'], ensure_ascii=False)} {v['count']}"
                    for v in s["top_values"]
                )
                lines.append(f"    取值分布（样本）：{values}")
        for fk in t["foreign_keys"]:
            ref = f"{fk['referenced_schema']}.{fk['referenced_table']}"
            cols, ref_cols = ", ".join(fk["columns"]), ", ".join(fk["referenced_columns"])
            lines.append(f"  外键 ({cols}) → {ref} ({ref_cols})")
        sample = [{k: _short(v) for k, v in row.items()} for row in t["sample"]]
        lines.append(f"  样本：{json.dumps(sample, ensure_ascii=False, default=str)}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
