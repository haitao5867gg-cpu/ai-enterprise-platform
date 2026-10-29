"""Profile source tables through a data source's connector (read-only MCP tools)."""

import asyncio
import json
from typing import Any

from aep.datasources import DataSource
from aep.etl.plan import KnownValues
from aep.tools.mcp import McpTools

MAX_TABLES = 20
SAMPLE_ROWS = 5
MAX_VALUE_CHARS = 80


async def profile_tables(
    tools: McpTools,
    source: DataSource,
    wanted: list[str] | None = None,
    sample_rows: int = SAMPLE_ROWS,
) -> list[dict[str, Any]]:
    """Structure, stats and ``sample_rows`` sample rows of each table (``wanted``:
    schema.table names)."""
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
            tools.call(url, "sample_rows", {**args, "limit": sample_rows}),
        )
        return {
            "name": f"{schema}.{table}",
            "columns": described["columns"],
            "foreign_keys": described["foreign_keys"],
            "estimated_row_count": stats.get("estimated_row_count"),
            "sample_row_count": stats.get("sample_row_count"),
            "sample_limit": stats.get("sample_limit"),
            "column_stats": stats["columns"],
            "sample": sample["rows"],
        }

    return list(await asyncio.gather(*(one(schema, table) for schema, table in names)))


def known_values(tables: list[dict[str, Any]]) -> dict[str, KnownValues]:
    """Per column name, every value the profile listed for it.

    Only columns whose values were all listed (none cut short) count. A table was read
    whole when its stats sample came back smaller than the limit. A name used in several
    tables merges their values, and is complete only if every table's is.
    """
    known: dict[str, KnownValues] = {}
    for t in tables:
        rows, limit = t.get("sample_row_count"), t.get("sample_limit")
        whole = isinstance(rows, int) and isinstance(limit, int) and rows < limit
        for s in t["column_stats"]:
            listed = s.get("top_values")
            if listed is None or any(v.get("truncated") for v in listed):
                continue
            if len(listed) != (s.get("distinct_count") or 0):
                continue
            values = frozenset(str(v["value"]).strip().lower() for v in listed)
            name, complete = s["name"].lower(), whole
            if name in known:
                values |= known[name].values
                complete = complete and known[name].complete
            known[name] = KnownValues(values, complete)
    return known


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "…"
    return value


def describe_for_model(tables: list[dict[str, Any]]) -> str:
    """A compact text view of the profile for the planning prompt."""
    parts = []
    for t in tables:
        stats = {c["name"]: c for c in t["column_stats"]}
        head = f"约 {t['estimated_row_count']} 行"
        rows, limit = t.get("sample_row_count"), t.get("sample_limit")
        if isinstance(rows, int) and isinstance(limit, int) and rows >= limit:
            # Otherwise a column's distinct count reads like the table's row count.
            head += f"；空值率、不同值和取值分布只统计了 {rows} 行样本"
        lines = [f"表 {t['name']}（{head}）"]
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
                    f"{json.dumps(v['value'], ensure_ascii=False)}"
                    f"{'…（已截断，不能当作取值使用）' if v.get('truncated') else ''} {v['count']}"
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
