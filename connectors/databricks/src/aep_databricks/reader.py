"""The tools' statements. Metadata comes from each catalog's information_schema (so a
catalog must be in Unity Catalog), data from the tables themselves; every statement is
built here from checked names and checked expressions (aep_databricks.sql)."""

import asyncio
import math
from typing import Any

from aep_databricks.config import Settings
from aep_databricks.executor import Executor, Result, make_executor
from aep_databricks.sql import (
    ConnectorError,
    Schema,
    Table,
    columns,
    expression,
    ident,
    literal,
)

# Low-cardinality columns (at most this many distinct values in the sample) also get
# their value counts, so a caller can see the real spellings of statuses and codes.
TOP_VALUES_MAX = 20
TOP_VALUE_CHARS = 80
NUMERIC = ("tinyint", "smallint", "int", "bigint", "float", "double", "decimal")
COMPLEX = ("array", "map", "struct", "variant")


def _where(where: str | None, what: str = "读取条件") -> str:
    return "" if where is None else f" WHERE ({expression(where, what)})"


def _named(found: list[str], name: str, what: str) -> str:
    """The table's own spelling of ``name`` (Unity Catalog names ignore case)."""
    for candidate in found:
        if candidate.lower() == name.lower():
            return candidate
    raise ConnectorError(f"{what}没有列 {name}")


class Reader:
    def __init__(self, settings: Settings, executor: Executor | None = None) -> None:
        self.settings = settings
        self._executor = executor
        self._allowed = {c.lower() for c in settings.databricks_read_catalogs}

    def executor(self) -> Executor:
        if self._executor is None:
            if not self.settings.databricks_read_host or not (
                self.settings.databricks_read_token.get_secret_value()
            ):
                raise ConnectorError("连接器没有配置 Databricks 工作区")
            self._executor = make_executor(self.settings)
        return self._executor

    def _catalog(self, catalog: str) -> str:
        if not self._allowed:
            raise ConnectorError("连接器没有配置允许读的 catalog（AEP_DATABRICKS_READ_CATALOGS）")
        if catalog.lower() not in self._allowed:
            raise ConnectorError(f"没有允许读 catalog {catalog}")
        return catalog

    async def run(self, statement: str) -> Result:
        return await self.executor().query(statement)

    async def close(self) -> None:
        if self._executor is not None:
            await self._executor.close()

    # Metadata

    async def _columns(self, table: Table) -> list[dict[str, Any]]:
        """The table's columns in order, with types, comments and primary-key positions."""
        self._catalog(table.catalog)
        info = f"{ident(table.catalog)}.information_schema"
        schema, name = literal(table.schema.lower()), literal(table.table.lower())
        result = await self.run(
            "SELECT c.column_name, lower(c.full_data_type) AS data_type, c.is_nullable, "
            "c.comment, k.ordinal_position AS key_position, t.comment AS table_comment "
            f"FROM {info}.columns AS c "
            f"JOIN {info}.tables AS t ON t.table_schema = c.table_schema "
            "AND t.table_name = c.table_name "
            "LEFT JOIN (SELECT u.column_name, u.ordinal_position "
            f"FROM {info}.table_constraints AS tc JOIN {info}.key_column_usage AS u "
            "ON u.constraint_schema = tc.constraint_schema "
            "AND u.constraint_name = tc.constraint_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            f"AND lower(tc.table_schema) = {schema} AND lower(tc.table_name) = {name}) AS k "
            "ON k.column_name = c.column_name "
            f"WHERE lower(c.table_schema) = {schema} AND lower(c.table_name) = {name} "
            "ORDER BY c.ordinal_position"
        )
        rows = result.dicts()
        if not rows:
            raise ConnectorError(f"表 {table.name} 不存在，或者没有读权限")
        return rows

    async def list_schemas(self) -> dict[str, Any]:
        if not self._allowed:
            self._catalog("")
        parts = [
            f"SELECT catalog_name, schema_name FROM {ident(catalog)}.information_schema.schemata "
            "WHERE schema_name <> 'information_schema'"
            for catalog in self.settings.databricks_read_catalogs
        ]
        result = await self.run(" UNION ALL ".join(parts) + " ORDER BY 1, 2")
        return {"schemas": [f"{r['catalog_name']}.{r['schema_name']}" for r in result.dicts()]}

    async def list_tables(self, schema: str) -> dict[str, Any]:
        found = Schema.parse(schema)
        self._catalog(found.catalog)
        result = await self.run(
            f"SELECT table_name FROM {ident(found.catalog)}.information_schema.tables "
            f"WHERE lower(table_schema) = {literal(found.schema.lower())} ORDER BY table_name"
        )
        return {"schema": found.name, "tables": [r["table_name"] for r in result.dicts()]}

    async def describe_table(self, schema: str, table: str) -> dict[str, Any]:
        found = Table.of(schema, table)
        rows = await self._columns(found)
        return {
            "schema": f"{found.catalog}.{found.schema}",
            "table": found.table,
            "comment": rows[0]["table_comment"],
            "columns": [
                {
                    "name": r["column_name"],
                    "data_type": r["data_type"],
                    "nullable": r["is_nullable"] == "YES",
                    "primary_key": r["key_position"] is not None,
                    "comment": r["comment"],
                }
                for r in rows
            ],
            "foreign_keys": await self._foreign_keys(found),
        }

    async def _foreign_keys(self, table: Table) -> list[dict[str, Any]]:
        info = f"{ident(table.catalog)}.information_schema"
        result = await self.run(
            "SELECT tc.constraint_name AS name, u.column_name, "
            "r.table_catalog AS ref_catalog, r.table_schema AS ref_schema, "
            "r.table_name AS ref_table, r.column_name AS ref_column "
            f"FROM {info}.table_constraints AS tc "
            f"JOIN {info}.key_column_usage AS u ON u.constraint_schema = tc.constraint_schema "
            "AND u.constraint_name = tc.constraint_name "
            f"JOIN {info}.referential_constraints AS rc "
            "ON rc.constraint_schema = tc.constraint_schema "
            "AND rc.constraint_name = tc.constraint_name "
            f"JOIN {info}.key_column_usage AS r "
            "ON r.constraint_catalog = rc.unique_constraint_catalog "
            "AND r.constraint_schema = rc.unique_constraint_schema "
            "AND r.constraint_name = rc.unique_constraint_name "
            "AND r.ordinal_position = u.position_in_unique_constraint "
            f"WHERE tc.constraint_type = 'FOREIGN KEY' AND lower(tc.table_schema) = "
            f"{literal(table.schema.lower())} AND lower(tc.table_name) = "
            f"{literal(table.table.lower())} ORDER BY tc.constraint_name, u.ordinal_position"
        )
        keys: dict[str, dict[str, Any]] = {}
        for row in result.dicts():
            key = keys.setdefault(
                row["name"],
                {
                    "name": row["name"],
                    "columns": [],
                    "referenced_schema": f"{row['ref_catalog']}.{row['ref_schema']}",
                    "referenced_table": row["ref_table"],
                    "referenced_columns": [],
                },
            )
            key["columns"].append(row["column_name"])
            key["referenced_columns"].append(row["ref_column"])
        return list(keys.values())

    # Rows

    async def sample_rows(
        self, schema: str, table: str, limit: int = 10, where: str | None = None
    ) -> dict[str, Any]:
        found = Table.of(schema, table)
        self._catalog(found.catalog)
        result = await self.run(f"SELECT * FROM {found.sql}{_where(where)} LIMIT {int(limit)}")
        return {
            "schema": f"{found.catalog}.{found.schema}",
            "table": found.table,
            "rows": result.dicts(),
        }

    async def read_rows(
        self,
        schema: str,
        table: str,
        offset: int = 0,
        limit: int = 1000,
        where: str | None = None,
    ) -> dict[str, Any]:
        found = Table.of(schema, table)
        condition = _where(where)
        described = await self._columns(found)
        key = sorted(
            (r for r in described if r["key_position"] is not None),
            key=lambda r: r["key_position"],
        )
        order = ", ".join(
            f"to_json({ident(r['column_name'])})"
            if not key and r["data_type"].startswith(COMPLEX)
            else ident(r["column_name"])
            for r in key or described
        )
        result = await self.run(
            f"SELECT * FROM {found.sql}{condition} ORDER BY {order} "
            f"LIMIT {int(limit) + 1} OFFSET {int(offset)}"
        )
        rows = result.dicts()
        return {
            "schema": f"{found.catalog}.{found.schema}",
            "table": found.table,
            "columns": [r["column_name"] for r in described],
            "rows": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    async def table_stats(
        self, schema: str, table: str, where: str | None = None
    ) -> dict[str, Any]:
        found = Table.of(schema, table)
        condition = _where(where)
        names = [r["column_name"] for r in await self._columns(found)]
        sample = self.settings.databricks_read_stats_sample
        cells = ", ".join(f"{i}, CAST({ident(name)} AS STRING)" for i, name in enumerate(names))
        # One pass over one sample: every column's cells, counted by value; the row count
        # with the condition comes along (no stats rows means no rows at all).
        result = await self.run(
            f"WITH sample AS (SELECT * FROM {found.sql}{condition} LIMIT {sample}), "
            f"cells AS (SELECT stack({len(names)}, {cells}) AS (__aep_column, __aep_value) "
            "FROM sample), "
            "counted AS (SELECT __aep_column, __aep_value, count(*) AS n FROM cells "
            "GROUP BY __aep_column, __aep_value), "
            "stats AS (SELECT __aep_column, count(__aep_value) AS distinct_count, "
            "sum(IF(__aep_value IS NULL, n, 0)) AS nulls, sum(n) AS total, "
            f"IF(count(__aep_value) <= {TOP_VALUES_MAX}, collect_list(IF(__aep_value IS NULL, "
            f"NULL, named_struct('value', left(__aep_value, {TOP_VALUE_CHARS}), "
            f"'truncated', length(__aep_value) > {TOP_VALUE_CHARS}, 'count', n))), NULL) AS top "
            "FROM counted GROUP BY __aep_column) "
            f"SELECT s.*, r.row_count FROM stats AS s CROSS JOIN "
            f"(SELECT count(*) AS row_count FROM {found.sql}{condition}) AS r"
        )
        by_column = {row["__aep_column"]: row for row in result.dicts()}
        any_row = next(iter(by_column.values()), None)
        total = int(any_row["total"]) if any_row else 0
        stats = []
        for i, name in enumerate(names):
            row = by_column.get(i)
            column: dict[str, Any] = {
                "name": name,
                "null_fraction": (row["nulls"] / total) if row and total else None,
                "distinct_count": int(row["distinct_count"]) if row else 0,
            }
            if row and row["top"] and 0 < column["distinct_count"] <= TOP_VALUES_MAX:
                values = []
                for item in row["top"]:
                    value = {"value": item["value"], "count": int(item["count"])}
                    if item.get("truncated"):
                        value["truncated"] = True
                    values.append(value)
                column["top_values"] = sorted(values, key=lambda v: (-v["count"], v["value"]))
            stats.append(column)
        return {
            "schema": f"{found.catalog}.{found.schema}",
            "table": found.table,
            "estimated_row_count": int(any_row["row_count"]) if any_row else 0,
            "sample_limit": sample,
            "sample_row_count": total,
            "columns": stats,
        }

    # Analysis

    def _table(self, value: str) -> Table:
        found = Table.parse(value)
        self._catalog(found.catalog)
        return found

    async def aggregate(
        self,
        table: str,
        expr: str,
        where: str | None = None,
        group_by: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        found = self._table(table)
        value = expression(expr, "聚合表达式")
        condition = _where(where)
        if not group_by:
            result = await self.run(f"SELECT ({value}) AS value FROM {found.sql}{condition}")
            return {"rows": [{"value": row[0]} for row in result.rows[:1]], "groups": 1}
        groups = columns(group_by, "分组列")
        if any(name.lower() == "value" for name in groups):
            raise ConnectorError("分组列不能叫 value（和结果里的 value 重名）")
        keys = ", ".join(ident(name) for name in groups)
        result = await self.run(
            f"SELECT *, count(*) OVER () AS __aep_groups FROM (SELECT {keys}, ({value}) "
            f"AS __aep_value FROM {found.sql}{condition} GROUP BY {keys}) "
            f"ORDER BY {keys} LIMIT {int(limit)}"
        )
        rows = result.dicts()
        return {
            "rows": [{**{n: r[n] for n in groups}, "value": r["__aep_value"]} for r in rows],
            "groups": int(rows[0]["__aep_groups"]) if rows else 0,
        }

    async def duplicates(
        self, table: str, key: list[str], where: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        found = self._table(table)
        names = columns(key, "列")
        if any(name.lower() == "count" for name in names):
            raise ConnectorError("列不能叫 count（和结果里的 count 重名）")
        keys = ", ".join(ident(name) for name in names)
        result = await self.run(
            f"SELECT * FROM (SELECT {keys}, count(*) AS __aep_count, "
            f"count(*) OVER () AS __aep_groups FROM {found.sql}{_where(where)} "
            f"GROUP BY {keys} HAVING count(*) > 1) "
            f"ORDER BY __aep_count DESC, {keys} LIMIT {int(limit)}"
        )
        rows = result.dicts()
        return {
            "groups": int(rows[0]["__aep_groups"]) if rows else 0,
            "rows": [{**{n: r[n] for n in names}, "count": int(r["__aep_count"])} for r in rows],
        }

    async def compare(
        self,
        table: str,
        expected: str,
        keys: list[str],
        compared: list[str] | None = None,
        where: str | None = None,
        expected_where: str | None = None,
        tolerance: float = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        left, right = self._table(table), self._table(expected)
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ConnectorError("tolerance 要是不小于 0 的数")
        left_where = _where(where)
        right_where = _where(expected_where, "expected_where ")
        key_names = columns(keys, "keys ")
        left_columns, right_columns = await asyncio.gather(
            self._columns(left), self._columns(right)
        )
        left_types = {r["column_name"]: r["data_type"] for r in left_columns}
        right_types = {r["column_name"]: r["data_type"] for r in right_columns}
        key_pairs = [
            (
                _named(list(left_types), k, f"{left.name} "),
                _named(list(right_types), k, f"{right.name} "),
            )
            for k in key_names
        ]
        if compared is None:
            lowered = {k.lower() for k in key_names}
            names = [
                n
                for n in left_types
                if n.lower() not in lowered and any(n.lower() == m.lower() for m in right_types)
            ]
        else:
            names = columns(compared, "columns ", most=200)
        pairs = [
            (
                _named(list(left_types), n, f"{left.name} "),
                _named(list(right_types), n, f"{right.name} "),
            )
            for n in names
        ]
        shown = [a for a, _ in pairs]

        # Keys first: with a repeated key the join would count the extra rows as matches.
        lk = ", ".join(ident(a) for a, _ in key_pairs)
        rk = ", ".join(ident(b) for _, b in key_pairs)
        result = await self.run(
            f"WITH d AS (SELECT 'table' AS __aep_side, {lk}, count(*) AS __aep_count "
            f"FROM {left.sql}{left_where} GROUP BY {lk} HAVING count(*) > 1 UNION ALL "
            f"SELECT 'expected', {rk}, count(*) FROM {right.sql}{right_where} "
            f"GROUP BY {rk} HAVING count(*) > 1) "
            "SELECT *, sum(IF(__aep_side = 'table', 1, 0)) OVER () AS __aep_t, "
            "sum(IF(__aep_side = 'expected', 1, 0)) OVER () AS __aep_e FROM d "
            f"ORDER BY __aep_count DESC, __aep_side LIMIT {int(limit)}"
        )
        rows = result.rows
        if rows:
            first = rows[0]
            return {
                "duplicates": {"table": int(first[-2]), "expected": int(first[-1])},
                "missing": None,
                "extra": None,
                "different": None,
                "columns": shown,
                "rows": [
                    {
                        "kind": "duplicate",
                        "side": row[0],
                        "key": {a: row[1 + i] for i, (a, _) in enumerate(key_pairs)},
                        "count": int(row[1 + len(key_pairs)]),
                    }
                    for row in rows
                ],
            }

        numeric = [
            left_types[a].startswith(NUMERIC) and right_types[b].startswith(NUMERIC)
            for a, b in pairs
        ]
        tol = repr(float(tolerance))
        # A null on one side only is a difference: abs(NULL - x) <= tol is NULL, and a NULL
        # here would make the whole row count as the same.
        same = [
            f"(a.{ident(a)} <=> b.{ident(b)}"
            + (f" OR coalesce(abs(a.{ident(a)} - b.{ident(b)}) <= {tol}, false))" if num else ")")
            for (a, b), num in zip(pairs, numeric, strict=True)
        ]
        differs = f"WHEN NOT ({' AND '.join(same)}) THEN 'different' " if same else ""
        on = " AND ".join(f"a.{ident(a)} <=> b.{ident(b)}" for a, b in key_pairs)
        out_keys = ", ".join(
            f"coalesce(a.{ident(a)}, b.{ident(b)}) AS __aep_k{i}"
            for i, (a, b) in enumerate(key_pairs)
        )
        out_values = "".join(
            f", a.{ident(a)} AS __aep_a{i}, b.{ident(b)} AS __aep_b{i}"
            for i, (a, b) in enumerate(pairs)
        )
        left_cols = ", ".join(ident(a) for a, _ in key_pairs + pairs)
        right_cols = ", ".join(ident(b) for _, b in key_pairs + pairs)
        order = ", ".join(f"__aep_k{i}" for i in range(len(key_pairs)))
        result = await self.run(
            f"WITH a AS (SELECT {left_cols}, 1 AS __aep_here FROM {left.sql}{left_where}), "
            f"b AS (SELECT {right_cols}, 1 AS __aep_here FROM {right.sql}{right_where}), "
            "j AS (SELECT CASE WHEN a.__aep_here IS NULL THEN 'missing' "
            f"WHEN b.__aep_here IS NULL THEN 'extra' {differs}END AS __aep_kind, "
            f"{out_keys}{out_values} FROM a FULL OUTER JOIN b ON {on}) "
            "SELECT *, sum(IF(__aep_kind = 'missing', 1, 0)) OVER () AS __aep_missing, "
            "sum(IF(__aep_kind = 'extra', 1, 0)) OVER () AS __aep_extra, "
            "sum(IF(__aep_kind = 'different', 1, 0)) OVER () AS __aep_different "
            f"FROM j WHERE __aep_kind IS NOT NULL ORDER BY __aep_kind, {order} LIMIT {int(limit)}"
        )
        found = result.dicts()
        examples = []
        for row in found:
            kind = row["__aep_kind"]
            left_values = {a: row[f"__aep_a{i}"] for i, (a, _) in enumerate(pairs)}
            right_values = {a: row[f"__aep_b{i}"] for i, (a, _) in enumerate(pairs)}
            example: dict[str, Any] = {
                "kind": kind,
                "key": {a: row[f"__aep_k{i}"] for i, (a, _) in enumerate(key_pairs)},
                "table": None if kind == "missing" else left_values,
                "expected": None if kind == "extra" else right_values,
            }
            if kind == "different":
                example["differs"] = [
                    a
                    for (a, _), num in zip(pairs, numeric, strict=True)
                    if not _same(left_values[a], right_values[a], num, tolerance)
                ]
            examples.append(example)
        first_row = found[0] if found else None
        return {
            "duplicates": {"table": 0, "expected": 0},
            "missing": int(first_row["__aep_missing"]) if first_row else 0,
            "extra": int(first_row["__aep_extra"]) if first_row else 0,
            "different": int(first_row["__aep_different"]) if first_row else 0,
            "columns": shown,
            "rows": examples,
        }


def _same(a: Any, b: Any, numeric: bool, tolerance: float) -> bool:
    if a == b:
        return True
    if numeric and isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b) <= tolerance
    return False
