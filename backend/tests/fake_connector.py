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
# carry comments, and the row tools take a ``where``: here comparisons of a column with a
# quoted text or a number (=, !=, <, <=, >, >=), parenthesised and joined with AND. The
# analysis tools (aggregate, duplicates, compare) take three-part names and work like the
# real connector's; ``aggregate`` knows count(*), and sum, min, max and count of a column.

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
_AGGREGATE = re.compile(r"^(sum|min|max|count)\((\*|`?\w+`?)\)$", re.IGNORECASE)


_TOKEN = re.compile(
    r"\s*(\(|\)|<=>|!=|<>|<=|>=|=|<|>|'(?:[^'\\]|\\.)*'|`[^`]*`|-?\d+(?:\.\d+)?|\w+)"
)
_OPS = ("<=>", "=", "!=", "<>", "<=", ">=", "<", ">")


def _atom(row: dict, column: str, op: str, literal: str) -> bool:
    """One comparison ``column op literal``; a null meets nothing but ``<=> NULL``."""
    value = row.get(column)
    if literal.upper() == "NULL":
        return op == "<=>" and value is None
    if literal.startswith("'"):
        wanted: object = literal[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    elif literal.lower() in ("true", "false"):
        wanted = literal.lower() == "true"
    else:
        wanted = float(literal)
    if value is None:
        return False
    if isinstance(wanted, float) and isinstance(value, str):
        return False
    if isinstance(wanted, str):
        value = str(value)
    if op in ("<=>", "="):
        return value == wanted
    return {
        "!=": value != wanted,
        "<>": value != wanted,
        "<": value < wanted,
        "<=": value <= wanted,
        ">": value > wanted,
        ">=": value >= wanted,
    }[op]


def _keep(row: dict, where: str | None) -> bool:
    """Whether ``row`` meets ``where``: comparisons of a column with a literal (or
    ``<=> NULL``), joined with AND, OR and NOT and grouped in parentheses."""
    if where is None:
        return True
    tokens: list[str] = []
    at = 0
    text = where.strip()
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None or not match.group(1):
            raise ToolError(f"读取条件写法不对：这个假连接器不认 {text[at:]}")
        tokens.append(match.group(1))
        at = match.end()
        while at < len(text) and text[at].isspace():
            at += 1
    pos = 0

    def peek() -> str:
        return tokens[pos] if pos < len(tokens) else ""

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def either() -> bool:
        found = both()
        while peek().upper() == "OR":
            take()
            found = both() or found
        return found

    def both() -> bool:
        found = negated()
        while peek().upper() == "AND":
            take()
            found = negated() and found
        return found

    def negated() -> bool:
        if peek().upper() == "NOT":
            take()
            return not negated()
        if peek() == "(":
            take()
            found = either()
            if take() != ")":
                raise ToolError(f"读取条件写法不对：这个假连接器不认 {where}")
            return found
        column = take().strip("`")
        op = take()
        if op not in _OPS:
            raise ToolError(f"读取条件写法不对：这个假连接器不认 {where}")
        return _atom(row, column, op, take())

    found = either()
    if pos != len(tokens):
        raise ToolError(f"读取条件写法不对：这个假连接器不认 {where}")
    return found


def _same(a: object, b: object, tolerance: float) -> bool:
    if a == b:
        return True
    numbers = (int, float)
    if isinstance(a, numbers) and isinstance(b, numbers) and not isinstance(a, bool):
        return abs(a - b) <= tolerance
    return False


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
        return [r for r in find(schema, name)["rows"] if _keep(r, where)]

    def table_rows(table: str, where: str | None) -> list[dict]:
        schema, _, name = table.rpartition(".")
        if schema.count(".") != 1:
            raise ToolError(f"表名要写成 catalog.schema.table，收到的是 {table!r}")
        return rows(schema, name, where)

    def looked_up(found: list[dict], lookup: dict | None) -> list[dict]:
        """The rows that do (``found``) or do not have a row in the lookup's table with the
        same values in the ``on`` columns (None matching None) that its ``where`` keeps."""
        if lookup is None:
            return found
        other = table_rows(lookup["table"], lookup.get("where"))
        pairs = list(lookup["on"].items())

        def value(v: object) -> object:
            return v.strip() if lookup.get("trim") and isinstance(v, str) else v

        def has(row: dict) -> bool:
            return any(all(value(row.get(a)) == value(o.get(b)) for a, b in pairs) for o in other)

        return [r for r in found if has(r) is lookup["found"]]

    def columns_of(table: str) -> list[str]:
        schema, _, name = table.rpartition(".")
        return [c["name"] for c in find(schema, name)["columns"]]

    def value_of(expr: str, found: list[dict]) -> object:
        match = _AGGREGATE.match(expr.strip())
        if match is None:
            raise ToolError(f"聚合表达式写法不对：这个假连接器不认 {expr}")
        how, column = match[1].lower(), match[2].strip("`")
        if column == "*":
            return len(found)
        present = [r[column] for r in found if r.get(column) is not None]
        if how == "count":
            return len(present)
        if not present:
            return None
        return {"sum": sum, "min": min, "max": max}[how](present)

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
    def sample_rows(
        schema: str,
        table: str,
        limit: int = 10,
        where: str | None = None,
        lookup: dict | None = None,
    ) -> dict:
        """At most 100 rows, those ``where`` (and ``lookup``) keeps."""
        args = {"schema": schema, "table": table, "where": where}
        seen.append(("sample_rows", args | ({"lookup": lookup} if lookup else {})))
        found = looked_up(rows(schema, table, where), lookup)
        return {"schema": schema, "table": table, "rows": found[:limit]}

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

    @server.tool()
    def aggregate(
        table: str,
        expr: str,
        where: str | None = None,
        group_by: list[str] | None = None,
        limit: int = 20,
        lookup: dict | None = None,
    ) -> dict:
        """One aggregate value, or one per group (sorted by the group columns)."""
        args = {"table": table, "expr": expr, "where": where, "group_by": group_by}
        seen.append(("aggregate", args | {"limit": limit} | ({"lookup": lookup} if lookup else {})))
        found = looked_up(table_rows(table, where), lookup)
        if not group_by:
            return {"rows": [{"value": value_of(expr, found)}], "groups": 1}
        groups: dict[tuple, list[dict]] = {}
        for row in found:
            groups.setdefault(tuple(row.get(c) for c in group_by), []).append(row)
        keys = sorted(groups, key=lambda k: [str(v) for v in k])
        out = [dict(zip(group_by, k, strict=True)) | {"value": value_of(expr, groups[k])}
               for k in keys]  # fmt: skip
        return {"rows": out[:limit], "groups": len(out)}

    @server.tool()
    def duplicates(
        table: str, columns: list[str], where: str | None = None, limit: int = 20
    ) -> dict:
        """Groups of rows that repeat ``columns``, most repeated first."""
        args = {"table": table, "columns": columns, "where": where, "limit": limit}
        seen.append(("duplicates", args))
        counts: dict[tuple, int] = {}
        for row in table_rows(table, where):
            key = tuple(row.get(c) for c in columns)
            counts[key] = counts.get(key, 0) + 1
        repeated = sorted(
            ((k, n) for k, n in counts.items() if n > 1), key=lambda x: (-x[1], str(x[0]))
        )
        return {
            "groups": len(repeated),
            "rows": [dict(zip(columns, k, strict=True)) | {"count": n} for k, n in repeated][
                :limit
            ],
        }

    @server.tool()
    def compare(
        table: str,
        expected: str,
        keys: list[str],
        columns: list[str] | None = None,
        where: str | None = None,
        expected_where: str | None = None,
        tolerance: float = 0,
        limit: int = 20,
    ) -> dict:
        """Row-by-row comparison on ``keys``; repeated keys stop it."""
        args = {
            "table": table,
            "expected": expected,
            "keys": keys,
            "columns": columns,
            "where": where,
            "expected_where": expected_where,
            "tolerance": tolerance,
            "limit": limit,
        }
        seen.append(("compare", args))
        mine, theirs = table_rows(table, where), table_rows(expected, expected_where)
        for name, side in ((table, columns_of(table)), (expected, columns_of(expected))):
            for column in [*keys, *(columns or [])]:
                if column not in side:
                    raise ToolError(f"{name} 没有列 {column}")
        if columns is None:
            other = columns_of(expected)
            columns = [c for c in columns_of(table) if c not in keys and c in other]

        def by_key(rows: list[dict]) -> tuple[dict[tuple, dict], dict[tuple, int]]:
            found: dict[tuple, dict] = {}
            counts: dict[tuple, int] = {}
            for row in rows:
                key = tuple(row.get(k) for k in keys)
                found[key] = row
                counts[key] = counts.get(key, 0) + 1
            return found, {k: n for k, n in counts.items() if n > 1}

        left, left_dups = by_key(mine)
        right, right_dups = by_key(theirs)
        if left_dups or right_dups:
            dup_rows = [("table", k, n) for k, n in left_dups.items()]
            dup_rows += [("expected", k, n) for k, n in right_dups.items()]
            dup_rows.sort(key=lambda x: (-x[2], x[0]))
            return {
                "duplicates": {"table": len(left_dups), "expected": len(right_dups)},
                "missing": None,
                "extra": None,
                "different": None,
                "columns": columns,
                "rows": [
                    {
                        "kind": "duplicate",
                        "side": side,
                        "key": dict(zip(keys, k, strict=True)),
                        "count": n,
                    }
                    for side, k, n in dup_rows[:limit]
                ],  # fmt: skip
            }
        examples = []
        counts = {"missing": 0, "extra": 0, "different": 0}
        for key in sorted(set(left) | set(right), key=lambda k: [str(v) for v in k]):
            a, b = left.get(key), right.get(key)
            kind = "missing" if a is None else "extra" if b is None else None
            differs = []
            if kind is None:
                differs = [c for c in columns if not _same(a.get(c), b.get(c), tolerance)]
                kind = "different" if differs else None
            if kind is None:
                continue
            counts[kind] += 1
            example = {
                "kind": kind,
                "key": dict(zip(keys, key, strict=True)),
                "table": None if a is None else {c: a.get(c) for c in columns},
                "expected": None if b is None else {c: b.get(c) for c in columns},
            }
            if kind == "different":
                example["differs"] = differs
            examples.append(example)
        order = {"different": 0, "extra": 1, "missing": 2}
        examples.sort(key=lambda e: order[e["kind"]])
        return {
            "duplicates": {"table": 0, "expected": 0},
            **counts,
            "columns": columns,
            "rows": examples[:limit],
        }

    return server
