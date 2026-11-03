"""Runs a generated pipeline's transformations on an existing Databricks cluster.

This file runs on Databricks and imports nothing from ``aep``. It evaluates each
target table like a pipeline update would: PySpark code runs with a stand-in
for ``pyspark.pipelines`` that collects the tables and their quality rules, SQL
targets are parsed from the pipeline file. A table read by another target is the
transformed result with its drop rules applied, as it would be stored. The
outcome is printed as one ``AEP_RESULT`` line of JSON; with ``spec["preview"]`` it
also has the first rows of each table that passed (sorted by each table's primary key
with ``spec["order"]``), and with ``spec["answer"]`` the answer a plan designs, worked
out when every table passed.

It is used two ways. ``aep.databricks`` sends it as source to the cluster with a
call to ``main`` appended, to check the code without writing anything. And a
bundle deployed as a job carries it as ``aep_harness.py`` next to the pipeline
file; the job's notebook calls ``run_notebook``, which writes each table that
passed its quality rules and fails the run when any table failed.

A run bound to a project (docs/api/data-nodes.md, "写到哪里，只写本项目的表" and
"写表方式") writes each table the way the plan says (overwrite, replace_where, append
or merge), only tables of the project: named with its prefix, and either new, marked
with the project (table property ``aep.project``) or adopted by it. The run parameters
reach PySpark code as ``spark.conf.get("aep_params")`` and SQL as ``${param.<key>}``.
"""

import json
import re
import sys
import types
from typing import Any

MARKER = "AEP_RESULT "
KINDS = {"expect": "warn", "expect_or_drop": "drop", "expect_or_fail": "fail"}
# The warnings a pipeline's code may report through warn(): how many, how long each.
WARNINGS_MAX = 20
WARNING_CHARS = 500
# A run parameter in a SQL text: a target's query, a replace_where condition, a check.
PARAM = re.compile(r"\$\{param\.([a-z][a-z0-9_]{0,31})\}")
# A parameter, or one already wrapped in single quotes as a whole.
QUOTED_PARAM = re.compile(r"(')?\$\{param\.([a-z][a-z0-9_]{0,31})\}(?(1)')")


def sql_literal(value: Any, kind: str = "text") -> str:
    """A run parameter's value as a Spark SQL literal: text quoted and escaped, a ``date``
    as ``DATE'2026-10-01'``, numbers as they are, ``true``, ``false`` and ``NULL``."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    # Spark reads backslash escapes in string literals; '' would be two literals joined.
    return f"DATE'{text}'" if kind == "date" else f"'{text}'"


def with_params(text: str, values: dict[str, Any], kinds: dict[str, str]) -> str:
    """``text`` with each ``${param.<key>}`` replaced by the value's SQL literal; raises
    ``ValueError`` for a key the run has no parameter for. One already in single quotes
    (``'${param.version_id}'``, as people and models often write it) stands for the
    value as text, so it does not get a second pair of quotes."""

    def swap(match: re.Match[str]) -> str:
        quoted, key = match[1], match[2]
        if key not in values:
            raise ValueError(f"没有运行参数 {key}")
        value = values[key]
        if quoted and value is not None:
            return sql_literal(value if isinstance(value, str) else json.dumps(value))
        return sql_literal(value, kinds.get(key, "text"))

    return QUOTED_PARAM.sub(swap, text)


# The SQL pipeline as aep.etl.codegen.render_sql writes it.
_SQL_TARGET = re.compile(
    r"^CREATE OR REFRESH MATERIALIZED VIEW (\w+)(?: \(\n(.*?)\n\))?\n"
    r"COMMENT '(?:[^']|'')*'\nAS\n(.*?);\n(?=\n|\Z)",
    re.S | re.M,
)
_SQL_RULE = re.compile(
    r"^  CONSTRAINT (\w+) EXPECT \((.*)\)( ON VIOLATION (?:DROP ROW|FAIL UPDATE))?,?$"
)
_SQL_ACTION = {None: "warn", " ON VIOLATION DROP ROW": "drop", " ON VIOLATION FAIL UPDATE": "fail"}


def parse_sql(pipeline: str) -> list[dict[str, Any]]:
    """The targets of a SQL pipeline: name, query and quality rules."""
    targets: list[dict[str, Any]] = []
    for match in _SQL_TARGET.finditer(pipeline):
        rules = []
        for line in (match[2] or "").splitlines():
            rule = _SQL_RULE.match(line)
            if rule is None:
                raise ValueError(f"{match[1]}: 读不懂这条质量规则：{line.strip()}")
            rules.append(
                {"name": rule[1], "condition": rule[2], "on_violation": _SQL_ACTION[rule[3]]}
            )
        targets.append({"name": match[1], "query": match[3], "rules": rules})
    return targets


class _Conf:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)


class _Reader:
    def __init__(self, real: Any, table: Any) -> None:
        self._real = real
        self._table = table

    def table(self, name: str) -> Any:
        return self._table(name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _Spark:
    """The session the generated code sees: its own conf, and reads of target tables."""

    def __init__(self, real: Any, conf: dict[str, str], table: Any) -> None:
        self._real = real
        self.conf = _Conf(conf)
        self.read = _Reader(real.read, table)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _UpstreamFailed(Exception):
    pass


def _error(name: str, exc: BaseException) -> str:
    # Spark's analysis errors go on with the query plan and a JVM stack trace;
    # the first line says what is wrong.
    first = next((line for line in str(exc).splitlines() if line.strip()), "")
    return f"{name}: {type(exc).__name__}: {first.strip()[:1000]}"


def _quote(*parts: str) -> str:
    return ".".join("`" + part.replace("`", "``") + "`" for part in parts)


# Column types (docs/api/data-nodes.md, "表结构变化"): written the way Spark writes a
# column's ``dataType.simpleString()``, so a plan's type compares with a result's.
SIMPLE_TYPES = {
    "string",
    "int",
    "bigint",
    "smallint",
    "tinyint",
    "double",
    "float",
    "boolean",
    "date",
    "timestamp",
    "timestamp_ntz",
    "binary",
}
TYPE_ALIASES = {
    "integer": "int",
    "long": "bigint",
    "short": "smallint",
    "byte": "tinyint",
    "real": "float",
    "bool": "boolean",
    "text": "string",
    "timestamp_ltz": "timestamp",
}
_TYPE_TOKEN = re.compile(r"[a-z_][a-z0-9_]*|\d+|[<>(),:]")


def spark_type(text: Any) -> str | None:
    """``text`` as Spark writes the type: lower case, no spaces, other names for the same
    type made one (INTEGER is int, VARCHAR(10) is string, DECIMAL is decimal(10,0));
    None when it is not a Spark SQL type."""
    if not isinstance(text, str):
        return None
    plain = text.strip().lower()
    tokens = _TYPE_TOKEN.findall(plain)
    if not tokens or "".join(tokens) != re.sub(r"\s+", "", plain):
        return None
    at = 0

    def take(expected: str | None = None) -> str:
        nonlocal at
        if at >= len(tokens) or (expected is not None and tokens[at] != expected):
            raise ValueError(expected)
        at += 1
        return tokens[at - 1]

    def number() -> int:
        token = take()
        if not token.isdigit():
            raise ValueError(token)
        return int(token)

    def one() -> str:
        word = take()
        word = TYPE_ALIASES.get(word, word)
        if word in SIMPLE_TYPES:
            return word
        if word in ("decimal", "numeric", "dec"):
            precision, scale = 10, 0
            if at < len(tokens) and tokens[at] == "(":
                take("(")
                precision = number()
                if tokens[at : at + 1] == [","]:
                    take(",")
                    scale = number()
                take(")")
            if not 1 <= precision <= 38 or scale > precision:
                raise ValueError(word)
            return f"decimal({precision},{scale})"
        if word in ("varchar", "char"):
            take("(")
            number()
            take(")")
            return "string"
        if word == "array":
            take("<")
            inner = one()
            take(">")
            return f"array<{inner}>"
        if word == "map":
            take("<")
            key = one()
            take(",")
            value = one()
            take(">")
            return f"map<{key},{value}>"
        if word == "struct":
            take("<")
            fields = []
            while True:
                name = take()
                take(":")
                fields.append(f"{name}:{one()}")
                if take() == ">":
                    break
            return f"struct<{','.join(fields)}>"
        raise ValueError(word)

    try:
        found = one()
    except (ValueError, IndexError):
        return None
    return found if at == len(tokens) else None


def type_family(kind: str) -> str:
    """What a column of type ``kind`` (as ``spark_type`` writes it) holds: numbers of any
    width, and timestamps with or without a zone, are each one family."""
    if kind in ("tinyint", "smallint", "int", "bigint", "float", "double"):
        return "number"
    if kind.startswith("decimal("):
        return "number"
    if kind in ("timestamp", "timestamp_ntz"):
        return "timestamp"
    return kind


class _Refused(Exception):
    """A table the run must not write, or a result its write mode cannot store."""


def _metric(metrics: dict[str, Any], *names: str) -> int | None:
    for name in names:
        try:
            return int(metrics[name])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _history(spark: Any, stored: str, mode: str) -> dict[str, Any]:
    """What the write just made of the table, from its Delta history."""
    found: dict[str, Any] = {"mode": mode, "version": None, "operation": None}
    try:
        row = spark.sql(f"DESCRIBE HISTORY {stored} LIMIT 1").collect()[0]
        metrics = dict(row["operationMetrics"] or {})
        found |= {"version": int(row["version"]), "operation": row["operation"]}
    except Exception:  # noqa: BLE001 - the history says how much was written, nothing more
        metrics = {}
    merged = found["operation"] == "MERGE"
    written = _metric(metrics, "numTargetRowsInserted" if merged else "numOutputRows")
    # Replacing rows rewrites the files they were in: the other rows of those files are
    # written back as they were and Delta counts them as output too (jadeflow S-9).
    copied = None if merged else _metric(metrics, "numCopiedRows")
    found |= {
        "rows_written": written - copied if written is not None and copied else written,
        "rows_updated": _metric(metrics, "numTargetRowsUpdated"),
        "rows_deleted": _metric(metrics, "numTargetRowsDeleted", "numDeletedRows"),
    }
    if copied:
        found["rows_copied"] = copied
    return found


def _new_columns(
    spark: Any, df: Any, stored: str, name: str, load: dict[str, Any]
) -> list[tuple[Any, str | None, str]]:
    """The columns a project's existing table gets before ``df`` is written to it with a
    mode that keeps its other rows (docs/api/data-nodes.md, "表结构变化"): each (field,
    the column it goes after or None for first, the position in SQL), in the order the
    plan lists them in ``load["add_columns"]``. The result must have every column of the
    table and no new column the plan did not confirm; raises ``_Refused`` otherwise. A
    confirmed column the table already has (an earlier run added it) is left alone."""
    table = spark.read.table(stored)
    have = {c.lower(): c for c in table.columns}
    kinds = {f.name.lower(): f.dataType.simpleString() for f in table.schema.fields}
    fields = {f.name.lower(): f for f in df.schema.fields}
    missing = [c for low, c in have.items() if low not in fields]
    if missing:
        raise _Refused(f"{name}: 结果里没有表里已有的列 {'、'.join(missing)}，没有写")
    # A column whose values are of another kind than the table's (text for numbers, a
    # list for text) would fail Delta's write or change what the column means.
    clashes = []
    for low, column in have.items():
        want = spark_type(kinds.get(low))
        got = spark_type(fields[low].dataType.simpleString())
        if want and got and type_family(want) != type_family(got):
            clashes.append(f"{column} 在表里是 {want.upper()}，结果里是 {got.upper()}")
    if clashes:
        raise _Refused(f"{name}: 列的类型和表里的不一致（{'；'.join(clashes)}），没有写")
    approved = [c for c in load.get("add_columns") or [] if isinstance(c, dict)]
    confirmed = {str(c.get("name") or "").lower() for c in approved}
    extra = [f.name for low, f in fields.items() if low not in have and low not in confirmed]
    if extra:
        raise _Refused(
            f"{name}: 结果比表多了列 {'、'.join(extra)}，确认的方案里没有要加这些列，没有写"
        )
    added: list[tuple[Any, str | None, str]] = []
    known = dict(have)
    for column in approved:
        field = fields.get(str(column.get("name") or "").lower())
        if field is None or field.name.lower() in known:
            continue
        # The type the person confirmed with the plan (plans confirmed before types
        # were asked for have none: the result's type is taken).
        confirmed = column.get("type")
        got = field.dataType.simpleString()
        if confirmed is not None and spark_type(got) != spark_type(confirmed):
            want = (spark_type(confirmed) or str(confirmed)).upper()
            raise _Refused(
                f"{name}: 确认的方案里新加的列 {field.name} 是 {want}，结果里是 {got.upper()}，"
                f"没有加列，也没有写：代码要让这一列是 {want}（需要时用 cast）"
            )
        after = column.get("after")
        if after is None:
            position = "FIRST"
        elif str(after).lower() in known:
            position = "AFTER " + _quote(known[str(after).lower()])
        else:
            raise _Refused(
                f"{name}: 方案要把列 {field.name} 加在 {after} 后面，表里没有列 {after}，没有写"
            )
        known[field.name.lower()] = field.name
        added.append((field, after, position))
    return added


def _store(spark: Any, df: Any, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Writes ``df`` to table ``name`` in ``spec["write"]`` (catalog, schema) the way
    ``spec["loads"]`` says (overwrite when it says nothing) and returns its Delta history.
    With ``spec["project"]`` only a table of the project is written, and the table is
    marked with the project and ``spec["run_id"]``; an existing table keeps its columns
    and only gets the ones the plan confirmed (``columns_added`` in the result). Raises
    ``_Refused`` with the reason when the table is not written."""
    write = spec["write"]
    stored = _quote(write["catalog"], write["schema"], name)
    shown = f"{write['catalog']}.{write['schema']}.{name}"
    load = (spec.get("loads") or {}).get(name) or {"mode": "overwrite"}
    mode = load["mode"]
    project = spec.get("project")
    if project and not name.startswith(project["table_prefix"]):
        raise _Refused(f"表 {shown} 不以项目的表名前缀 {project['table_prefix']} 开头，没有写")
    exists = spark.catalog.tableExists(stored)
    if project and exists:
        rows = spark.sql(f"SHOW TBLPROPERTIES {stored}").collect()
        mark = {row["key"]: row["value"] for row in rows}.get("aep.project")
        if mark != project["id"] and name not in project["adopted"]:
            why = (
                f"项目标记是另一个项目 {mark}" if mark else "没有项目标记，也没有登记在“已有的表”里"
            )
            raise _Refused(f"表 {shown} 已经存在，不属于项目「{project['name']}」（{why}），没有写")
    # A project's existing table changes its columns only as the confirmed plan says; an
    # unbound run's write takes in whatever new columns the result has.
    keeping = exists and mode in ("replace_where", "append", "merge")
    guarded = bool(project) and keeping
    new = _new_columns(spark, df, stored, name, load) if guarded else []
    condition = ""
    if mode == "replace_where":
        condition = with_params(
            load["where"], spec.get("params") or {}, spec.get("param_types") or {}
        )
        outside = df.filter(f"NOT coalesce(({condition}), false)").count()
        if outside:
            raise _Refused(f"{name}: 有 {outside} 行不满足替换条件 {condition}，没有写")
    elif mode == "merge":
        keys = list(load["keys"])
        repeated = df.groupBy(*keys).count().filter("count > 1").count()
        if repeated:
            raise _Refused(f"{name}: 合并键 {'、'.join(keys)} 有 {repeated} 组重复，没有写")
    added: list[dict[str, Any]] = []
    for field, after, position in new:
        kind = field.dataType.simpleString()
        spark.sql(f"ALTER TABLE {stored} ADD COLUMNS ({_quote(field.name)} {kind} {position})")
        added.append({"name": field.name, "type": kind, "after": after})

    def write() -> None:
        if mode == "replace_where":
            writer = df.write.mode("overwrite")
            if exists:
                writer = writer.option("replaceWhere", condition)
                if not guarded:
                    writer = writer.option("mergeSchema", "true")
            writer.saveAsTable(stored)
        elif mode == "append":
            writer = df.write.mode("append")
            if not guarded:
                writer = writer.option("mergeSchema", "true")
            writer.saveAsTable(stored)
        elif mode == "merge":
            if not exists:
                df.write.saveAsTable(stored)
            else:
                if not guarded:
                    have = {c.lower() for c in spark.read.table(stored).columns}
                    more = [f for f in df.schema.fields if f.name.lower() not in have]
                    if more:
                        columns = ", ".join(
                            f"{_quote(f.name)} {f.dataType.simpleString()}" for f in more
                        )
                        spark.sql(f"ALTER TABLE {stored} ADD COLUMNS ({columns})")
                view = f"aep_merge_{name}"
                df.createOrReplaceTempView(view)
                on = " AND ".join(f"t.{_quote(k)} <=> s.{_quote(k)}" for k in keys)
                spark.sql(
                    f"MERGE INTO {stored} AS t USING {view} AS s ON {on} "
                    "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
                )
                spark.catalog.dropTempView(view)
        else:
            df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(stored)

    try:
        write()
    except Exception as exc:
        if not added:
            raise
        columns = "、".join(c["name"] for c in added)
        raise _Refused(_error(name, exc) + f"（列 {columns} 已经加到表里，数据没有写）") from exc
    found = _history(spark, stored, mode)
    if added:
        found["columns_added"] = added
    if project:
        marks = f"'aep.project' = {sql_literal(project['id'])}, "
        marks += f"'aep.run_id' = {sql_literal(spec.get('run_id') or '')}"
        try:
            spark.sql(f"ALTER TABLE {stored} SET TBLPROPERTIES ({marks})")
        except Exception as exc:  # noqa: BLE001
            raise _Refused(_error(name, exc) + "（数据已经写入，但没能打上项目标记）") from exc
    return found


def main(spark: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Evaluates every target; with ``spec["write"]`` (catalog, schema) also stores them.

    ``errors`` in the outcome come from the generated code or its quality rules,
    ``write_errors`` from storing a table that passed (permissions, the schema).
    """
    from pyspark.sql import functions as F

    defs: dict[str, tuple[Any, list[tuple[str, str, str]]]] = {}
    done: dict[str, Any] = {}
    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    write_errors: list[str] = []
    write = spec.get("write")
    if write:
        try:
            spark.sql(f"CREATE SCHEMA IF NOT EXISTS {_quote(write['catalog'], write['schema'])}")
        except Exception:  # noqa: BLE001 - an existing schema may still be written to
            pass

    def table(name: str) -> Any:
        if name not in defs:
            return spark.read.table(name)
        if name in results and name not in done:
            raise RuntimeError(f"{name} 和读它的表互相引用")
        if name not in done:
            done[name] = materialize(name)
        if done[name] is None:
            raise _UpstreamFailed(name)
        return done[name]

    def materialize(name: str) -> Any:
        build, rules = defs[name]
        result: dict[str, Any] = {"name": name, "status": "FAILED", "rows": None, "dropped": 0}
        results[name] = result
        try:
            df = build()
            ok = [F.coalesce(F.expr(cond), F.lit(False)) for _, cond, _ in rules]
            keep = F.lit(True)
            for (_, _, kind), passed in zip(rules, ok, strict=True):
                if kind == "drop":
                    keep = keep & passed
            stats = df.agg(
                F.count(F.lit(1)).alias("total"),
                F.sum(F.when(keep, 0).otherwise(1)).alias("dropped"),
                *[F.sum(F.when(p, 0).otherwise(1)).alias(f"failed_{i}") for i, p in enumerate(ok)],
            ).collect()[0]
        except _UpstreamFailed:
            result["status"] = "SKIPPED"  # its upstream table already reported the error
            return None
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            errors.append(_error(name, exc))
            return None
        total = int(stats["total"])
        result["expectations"] = [
            {"name": rule, "passed": total - int(stats[f"failed_{i}"] or 0),
             "failed": int(stats[f"failed_{i}"] or 0)}
            for i, (rule, _, _) in enumerate(rules)
        ]  # fmt: skip
        result["dropped"] = int(stats["dropped"] or 0)
        failing = [
            (rule, int(stats[f"failed_{i}"] or 0))
            for i, (rule, _, kind) in enumerate(rules)
            if kind == "fail" and stats[f"failed_{i}"]
        ]
        if failing:
            errors.extend(
                f"{name}: 质量规则 {r} 要求整次更新失败，有 {n} 行违反" for r, n in failing
            )
            return None
        kept = df.filter(keep)
        if write:
            try:
                result["write"] = _store(spark, kept, name, spec)
            except _Refused as exc:
                write_errors.append(str(exc))
                return None
            except Exception as exc:  # noqa: BLE001
                write_errors.append(_error(name, exc))
                return None
            # What later tables read is the table as stored (with the rows of earlier
            # runs that replace_where, append and merge keep).
            kept = spark.read.table(_quote(write["catalog"], write["schema"], name))
        result["status"] = "COMPLETED"
        result["rows"] = total - result["dropped"]
        return kept

    params = spec.get("params") or {}
    kinds = spec.get("param_types") or {}
    conf = {"source_catalog": spec["source_catalog"], "aep_params": json.dumps(params)}
    namespace: dict[str, Any] = {}
    if spec["language"] == "pyspark":
        dp = types.ModuleType("pyspark.pipelines")

        def materialized_view(name: str | None = None, **_: Any) -> Any:
            def wrap(fn: Any) -> Any:
                defs[name or fn.__name__] = (fn, defs.get(fn.__name__, (None, []))[1])
                return fn

            return wrap

        def rule(kind: str) -> Any:
            def decorator(rule_name: str, condition: str) -> Any:
                def wrap(fn: Any) -> Any:
                    # Decorators apply bottom-up; rules sit under materialized_view.
                    defs.setdefault(fn.__name__, (None, []))[1].insert(
                        0, (rule_name, condition, kind)
                    )
                    return fn

                return wrap

            return decorator

        dp.materialized_view = materialized_view  # type: ignore[attr-defined]
        for fn_name, kind in KINDS.items():
            setattr(dp, fn_name, rule(kind))
        import pyspark

        saved = sys.modules.get("pyspark.pipelines"), getattr(pyspark, "pipelines", None)
        sys.modules["pyspark.pipelines"] = dp
        pyspark.pipelines = dp  # type: ignore[attr-defined]
        try:
            namespace["spark"] = _Spark(spark, conf, table)
            exec(compile(spec["code"], "pipeline.py", "exec"), namespace)
        except Exception as exc:  # noqa: BLE001
            errors.append(_error("pipeline.py", exc))
        finally:
            if saved[0] is None:
                sys.modules.pop("pyspark.pipelines", None)
            else:
                sys.modules["pyspark.pipelines"] = saved[0]
            if saved[1] is None:
                delattr(pyspark, "pipelines")
            else:
                pyspark.pipelines = saved[1]  # type: ignore[attr-defined]
        for leftover in [k for k, v in defs.items() if v[0] is None]:
            del defs[leftover]
    else:
        catalog = spec["source_catalog"]
        names = [t["name"] for t in spec["targets"]]
        for target in spec["targets"]:
            query = target["query"].replace("${source_catalog}", catalog)
            rules = [(r["name"], r["condition"], r["on_violation"]) for r in target["rules"]]
            # A SQL target names the targets it reads in its text, by their bare names.
            upstream = [
                other
                for other in names
                if other != target["name"] and re.search(rf"(?<![\w.`]){re.escape(other)}\b", query)
            ]

            def build(query: str = query, upstream: list[str] = upstream) -> Any:
                for other in upstream:
                    table(other).createOrReplaceTempView(other)
                return spark.sql(with_params(query, params, kinds))

            defs[target["name"]] = (build, rules)

    for name in list(defs):
        if name not in done:
            done[name] = materialize(name)

    outcome = {"tables": list(results.values()), "errors": errors, "write_errors": write_errors}
    # What the generated code passed to warn() (the pipeline file's WARNINGS).
    warned = namespace.get("WARNINGS")
    if isinstance(warned, list) and warned:
        outcome["warnings"] = [str(w)[:WARNING_CHARS] for w in warned[:WARNINGS_MAX]]
    # How many rows the project's read filters left out (the pipeline file's FILTERED).
    filtered = namespace.get("FILTERED")
    if isinstance(filtered, dict) and filtered:
        outcome["filtered"] = {
            str(k)[:200]: v for k, v in list(filtered.items())[:WARNINGS_MAX] if type(v) is int
        }
    if spec.get("preview"):
        outcome["preview"] = _preview(done, spec["preview"], spec.get("order"))
    if spec.get("answer") and not errors and not write_errors:
        outcome["answer"] = _answer_of(done, spec["answer"])
    print(MARKER + json.dumps(outcome))
    return outcome


def _preview(
    done: dict[str, Any], rows: int, order: dict[str, list[str]] | None
) -> list[dict[str, Any]]:
    """The first ``rows`` rows of each table that passed, values as JSON writes them;
    with ``order`` (table name to primary key) sorted by the key, saying so in
    ``sorted_by``."""
    out = []
    for name, df in done.items():
        if df is None:
            continue
        keys = None if order is None else order.get(name) or []
        try:
            columns, found, sorted_by = _first_rows(df, rows, keys)
        except Exception:  # noqa: BLE001 - a preview is a nicety, the outcome stands
            continue
        table = {"table": name, "columns": columns, "rows": found}
        out.append(table if sorted_by is None else table | {"sorted_by": sorted_by})
    return out


def _answer_of(done: dict[str, Any], design: dict[str, Any]) -> dict[str, Any]:
    """The answer ``design`` asks of its table, as the transformations left it."""
    df = done.get(design["table"])
    if df is None:
        return {"error": f"试跑结果里没有表 {design['table']}"}
    return _answer(df, design)


# --- Reading rows and working out answers. cluster_harness.py and cluster_tasks.py each
# carry this part word for word (a test compares them): neither may import the other.

PLAIN_TIME = {"timestampFormat": "yyyy-MM-dd HH:mm:ss", "timestampNTZFormat": "yyyy-MM-dd HH:mm:ss"}
INTEGER_TYPES = ("tinyint", "smallint", "int", "bigint")


def _json_rows(df: Any, columns: list[str]) -> list[dict[str, Any]]:
    """``df``'s rows with their values converted by Spark's own JSON writer
    (``DataFrame.toJSON`` is missing under Spark Connect, ``to_json`` is not): dates as
    ``yyyy-MM-dd``, timestamps as the source had them, without a zone (an ingested
    ``2026-05-31 13:53:10`` would otherwise read ``2026-05-31T13:53:10.000Z``),
    decimals as numbers."""
    from pyspark.sql import functions as F

    row = F.to_json(F.struct(*[df[c] for c in columns]), PLAIN_TIME)
    return [{c: json.loads(r[0]).get(c) for c in columns} for r in df.select(row).collect()]


def _first_rows(
    df: Any, n: int, order: list[str] | None = None
) -> tuple[list[str], list[dict[str, Any]], list[str] | None]:
    """``df``'s columns, its first ``n`` rows and the columns they are sorted by. With
    ``order`` (a primary key) the rows are sorted by it, smallest first and empty values
    first; a key with a column the table lacks sorts nothing (``[]``). Without ``order``
    nothing is sorted and the third item is None."""
    columns = list(df.columns)
    sorted_by = None
    if order is not None:
        sorted_by = list(order) if all(c in columns for c in order) else []
        if sorted_by:
            df = df.orderBy(*[df[c].asc_nulls_first() for c in sorted_by])
    return columns, _json_rows(df.limit(n), columns), sorted_by


def _measure(frame: Any, measure: dict[str, Any]) -> Any:
    """The aggregate of one measure over ``frame``'s rows (a group, or the whole period)."""
    from pyspark.sql import functions as F

    name = measure["name"]
    if measure["agg"] == "sum":
        return F.sum(frame[name]).alias(name)
    if measure["agg"] == "ratio":
        numerator = F.sum(frame[measure["numerator"]])
        denominator = F.sum(frame[measure["denominator"]])
        # Divided only where the denominator is neither 0 nor empty.
        return F.when(denominator != 0, numerator / denominator).alias(name)
    return F.when(F.count(F.lit(1)) == 1, F.first(frame[name])).alias(name)


def _answer(df: Any, design: dict[str, Any]) -> dict[str, Any]:
    """The answer ``design`` asks of ``df`` (docs/api/data-nodes.md, "答案怎么算"):
    ``period``, ``rows``, ``totals``, ``mins``, ``groups``, ``row_count`` and the type of
    each column, values as JSON writes them, never rounded; ``{"error": ...}`` when a
    column is missing or Spark fails. ``design`` is data: ``filter`` (the column whose
    latest value is the period, or None), ``group_by``, ``measures`` (``name``, ``agg``
    and for a ratio ``numerator`` and ``denominator``), ``order_by`` and ``top_n``."""
    from pyspark.sql import functions as F

    try:
        latest = design.get("filter")
        group_by = list(design["group_by"])
        measures = list(design["measures"])
        names = [m["name"] for m in measures]
        wanted = [latest] if latest else []
        wanted += group_by + names + [o["column"] for o in design["order_by"]]
        wanted += [
            m[k] for m in measures if m["agg"] == "ratio" for k in ("numerator", "denominator")
        ]
        missing = [c for c in dict.fromkeys(wanted) if c not in df.columns]
        if missing:
            return {"error": "结果表里没有列 " + "、".join(missing)}
        row_count = df.count()
        period = None
        picked = df
        if latest:
            top = df.agg(F.max(df[latest]).alias(latest))
            period = _json_rows(top, [latest])[0][latest]
            # A join keeps the column's own type (dates, timestamps without a zone).
            picked = df.join(F.broadcast(top), on=latest, how="inner")
        exprs = [_measure(picked, m) for m in measures]
        grouped = picked.groupBy(*[picked[c] for c in group_by]).agg(*exprs)
        columns = group_by + names
        types = {c: grouped.schema[c].dataType.simpleString() for c in columns}
        if latest:
            types[latest] = df.schema[latest].dataType.simpleString()
        summed = [m["name"] for m in measures if m["agg"] == "sum"]
        if row_count == 0 or (latest and period is None):
            return {
                "period": None,
                "rows": [],
                "totals": {name: None for name in names},
                "mins": {name: None for name in summed},
                "groups": 0,
                "row_count": row_count,
                "types": types,
            }
        groups = grouped.count()
        ordering = [
            grouped[o["column"]].desc_nulls_last()
            if o["desc"]
            else grouped[o["column"]].asc_nulls_last()
            for o in design["order_by"]
        ]
        ordering += [grouped[c].asc_nulls_first() for c in group_by]
        rows = _json_rows(grouped.orderBy(*ordering).limit(design["top_n"]), columns)
        added = [m for m in measures if m["agg"] != "none"]
        totals: dict[str, Any] = {name: None for name in names}
        if added:
            whole = picked.agg(*[_measure(picked, m) for m in added])
            totals |= _json_rows(whole, [m["name"] for m in added])[0]
        mins: dict[str, Any] = {}
        if summed:
            least = grouped.agg(*[F.min(grouped[name]).alias(name) for name in summed])
            mins = _json_rows(least, summed)[0]
    except Exception as exc:  # noqa: BLE001 - reported with the answer, the run stands
        return {"error": _error(design.get("target") or "答案", exc)}
    return {
        "period": period,
        "rows": rows,
        "totals": totals,
        "mins": mins,
        "groups": groups,
        "row_count": row_count,
        "types": types,
    }


# --- End of the part both programs carry.


def run_notebook(
    spark: Any, dbutils: Any, library: str, settings: dict[str, Any] | None = None
) -> None:
    """The job notebook's body: runs ``library`` (a path next to the notebook).

    The notebook's widgets name the source catalog and where the tables go, and for a
    run bound to a project the project (``project``, ``project_name``, ``table_prefix``,
    ``adopted``), the run parameters (``params``) and the run (``run_id``). ``settings``
    are what the bundle fixed: each table's write mode (``loads``) and the parameters'
    types (``param_types``). A failed table fails the run, with the outcome in the
    error message.
    """

    def arg(name: str) -> str:
        try:
            return dbutils.widgets.get(name) or ""
        except Exception:  # noqa: BLE001 - a job deployed before the widget existed
            return ""

    with open(library, encoding="utf-8") as f:  # notebooks run in their own folder
        code = f.read()
    spec: dict[str, Any] = {
        "source_catalog": arg("source_catalog"),
        "write": {"catalog": arg("catalog"), "schema": arg("schema")},
        "loads": (settings or {}).get("loads") or {},
        "param_types": (settings or {}).get("param_types") or {},
        "params": json.loads(arg("params") or "{}"),
        "run_id": arg("run_id"),
    }
    if arg("project"):
        spec["project"] = {
            "id": arg("project"),
            "name": arg("project_name") or arg("project"),
            "table_prefix": arg("table_prefix"),
            "adopted": json.loads(arg("adopted") or "[]"),
        }
    if library.endswith(".sql"):
        spec |= {"language": "sql", "targets": parse_sql(code)}
    else:
        spec |= {"language": "pyspark", "code": code}
    outcome = main(spark, spec)
    if outcome["errors"] or outcome["write_errors"]:
        raise RuntimeError(MARKER + json.dumps(outcome))
    dbutils.notebook.exit(MARKER + json.dumps(outcome))
