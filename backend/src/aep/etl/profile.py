"""Profile source tables through a data source's connector (read-only MCP tools)."""

import asyncio
import json
from typing import Any

from aep.datasources import DataSource
from aep.etl.plan import KnownValues
from aep.policy import LABELS, mask
from aep.tools.mcp import McpTools

MAX_TABLES = 20
SAMPLE_ROWS = 5
MAX_VALUE_CHARS = 80


class InputError(ValueError):
    """The run's input is wrong (no such data source, bad table names): trying again
    gives the same failure."""


def split_name(source: DataSource, name: str) -> tuple[str, str]:
    """``(schema, table)`` of a table name as the source's connector takes it: the table
    is after the last dot. A Databricks table is catalog.schema.table, so its schema is
    catalog.schema; other sources' tables are schema.table."""
    schema, _, table = name.rpartition(".")
    if source.kind == "databricks":
        catalog, dot, inner = schema.partition(".")
        if not (catalog and dot and inner and "." not in inner and table):
            raise InputError(f"表 {name} 要写成 catalog.schema.table")
    elif not (schema and table) or "." in schema:
        raise InputError(f"表 {name} 要写成 schema.table")
    return schema, table


async def profile_tables(
    tools: McpTools,
    source: DataSource,
    wanted: list[str] | None = None,
    sample_rows: int = SAMPLE_ROWS,
    *,
    filters: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Structure, stats and ``sample_rows`` sample rows of each table (``wanted``: names
    as ``split_name`` takes them). ``filters`` (table name: Spark SQL condition, any case)
    limits the stats and the sample of a table to the rows the condition keeps; it goes
    to the connector as ``where`` (connectors/databricks)."""
    url = source.connector_url

    def call(tool: str, args: dict[str, Any] | None = None) -> Any:
        return tools.call(url, tool, args or {}, timeout_seconds)

    if wanted:
        names = [split_name(source, name) for name in wanted]
    else:
        names = []
        for schema in (await call("list_schemas"))["schemas"]:
            listed = await call("list_tables", {"schema": schema})
            names += [(schema, table) for table in listed["tables"]]
    if not names:
        raise InputError(f"数据源 {source.name} 里没有可读的表")
    if len(names) > MAX_TABLES:
        raise InputError(f"数据源里有 {len(names)} 张表，一次最多 {MAX_TABLES} 张，请指定 tables")

    wheres = {name.lower(): where for name, where in (filters or {}).items() if where}

    async def one(schema: str, table: str) -> dict[str, Any]:
        args = {"schema": schema, "table": table}
        where = wheres.get(f"{schema}.{table}".lower())
        rows = {"where": where} if where else {}
        described, stats, sample = await asyncio.gather(
            call("describe_table", args),
            call("table_stats", {**args, **rows}),
            call("sample_rows", {**args, **rows, "limit": sample_rows}),
        )
        return {
            "name": f"{schema}.{table}",
            "comment": described.get("comment"),
            "filter": where,
            "columns": [{**c, "comment": c.get("comment")} for c in described["columns"]],
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


def seen_values(tables: list[dict[str, Any]]) -> dict[str, dict[str, frozenset[str] | None]]:
    """Per table (schema.table) and column name, the values the profile listed, trimmed and
    lower-cased, when it listed them all (none cut short, as many as the distinct count);
    None for a column whose values were not all listed, such as a time or an amount."""
    seen: dict[str, dict[str, frozenset[str] | None]] = {}
    for t in tables:
        columns: dict[str, frozenset[str] | None] = {}
        for s in t.get("column_stats") or []:
            listed = s.get("top_values")
            whole = (
                listed is not None
                and not any(v.get("truncated") for v in listed)
                and len(listed) == (s.get("distinct_count") or 0)
            )
            values = frozenset(str(v["value"]).strip().lower() for v in listed or [])
            columns[s["name"].lower()] = values if whole else None
        seen[t["name"]] = columns
    return seen


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "…"
    return value


def describe_for_model(
    tables: list[dict[str, Any]],
    samples: str = "full",
    run_id: str = "",
    references: list[dict[str, Any]] | None = None,
) -> str:
    """A compact text view of the profile for the planning prompt, with as much of the
    data as the data outbound policy's ``samples`` level lets go to a model (aep.policy):
    ``masked`` masks the sample rows and value counts, ``stats`` leaves them out,
    ``schema`` also leaves out row counts, null fractions and distinct counts. Comments,
    the read filter and what the project says of a table it references (``references``:
    the project's, aep.projects) are what people wrote, not data, so they always go."""
    noted = {str(r.get("name", "")).lower(): r for r in references or []}
    values = samples in ("full", "masked")
    counts = samples != "schema"

    def shown(value: Any) -> Any:
        return mask(value, run_id) if samples == "masked" else value

    parts = []
    if samples != "full":
        left = {
            "masked": "样本和取值分布已脱敏：文字换成了记号 ‹文本#…›，"
            "数字只留一位有效数字，日期只留到月；写条件时照抄记号，平台会换回原值",
            "stats": "没有样本和取值分布",
            "schema": "只有表结构，没有行数、统计、样本和取值分布",
        }[samples]
        parts.append(f"（按数据外发策略「{LABELS[samples]}」：{left}）")
    for t in tables:
        stats = {c["name"]: c for c in t["column_stats"]}
        head = f"约 {t['estimated_row_count']} 行"
        rows, limit = t.get("sample_row_count"), t.get("sample_limit")
        if isinstance(rows, int) and isinstance(limit, int) and rows >= limit:
            # Otherwise a column's distinct count reads like the table's row count.
            head += f"；空值率、不同值和取值分布只统计了 {rows} 行样本"
        lines = [f"表 {t['name']}（{head}）" if counts else f"表 {t['name']}"]
        if t.get("comment"):
            lines.append(f"  表注释：{t['comment']}")
        reference = noted.get(t["name"].lower())
        if reference:
            said = reference.get("description") or ""
            lines.append(
                f"  项目引用表「{reference.get('label') or t['name']}」"
                + (f"：{said}" if said else "")
            )
        if t.get("filter"):
            # The stats and the sample were read with it too.
            lines.append(f"  只读 {t['filter']} 的行（项目登记的读取筛选，统计和样本也只算这些行）")
        for col in t["columns"]:
            s = stats.get(col["name"], {})
            flags = [col["data_type"]]
            if col.get("primary_key"):
                flags.append("主键")
            if not col.get("nullable", True):
                flags.append("非空")
            if s and counts:
                flags.append(f"空值率 {s.get('null_fraction') or 0:.0%}")
                flags.append(f"不同值 {s.get('distinct_count')}")
            note = f"；说明：{col['comment']}" if col.get("comment") else ""
            lines.append(f"  - {col['name']}：{'，'.join(flags)}{note}")
            if s.get("top_values") and values:
                listed = "，".join(
                    f"{json.dumps(shown(v['value']), ensure_ascii=False)}"
                    f"{'…（已截断，不能当作取值使用）' if v.get('truncated') else ''} {v['count']}"
                    for v in s["top_values"]
                )
                lines.append(f"    取值分布（样本）：{listed}")
        for fk in t["foreign_keys"]:
            ref = f"{fk['referenced_schema']}.{fk['referenced_table']}"
            cols, ref_cols = ", ".join(fk["columns"]), ", ".join(fk["referenced_columns"])
            lines.append(f"  外键 ({cols}) → {ref} ({ref_cols})")
        if values:
            # A token stands for the whole value, so the model can copy it back.
            sample = [
                {k: shown(v) if samples == "masked" else _short(v) for k, v in row.items()}
                for row in t["sample"]
            ]
            lines.append(f"  样本：{json.dumps(sample, ensure_ascii=False, default=str)}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
