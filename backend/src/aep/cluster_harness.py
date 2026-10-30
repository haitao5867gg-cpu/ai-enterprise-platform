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
"""

import json
import re
import sys
import types
from typing import Any

MARKER = "AEP_RESULT "
KINDS = {"expect": "warn", "expect_or_drop": "drop", "expect_or_fail": "fail"}

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
            stored = _quote(write["catalog"], write["schema"], name)
            try:
                kept.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(stored)
            except Exception as exc:  # noqa: BLE001
                write_errors.append(_error(name, exc))
                return None
            kept = spark.read.table(stored)
        result["status"] = "COMPLETED"
        result["rows"] = total - result["dropped"]
        return kept

    conf = {"source_catalog": spec["source_catalog"]}
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
            namespace = {"spark": _Spark(spark, conf, table)}
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
                return spark.sql(query)

            defs[target["name"]] = (build, rules)

    for name in list(defs):
        if name not in done:
            done[name] = materialize(name)

    outcome = {"tables": list(results.values()), "errors": errors, "write_errors": write_errors}
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


def run_notebook(spark: Any, dbutils: Any, library: str) -> None:
    """The job notebook's body: runs ``library`` (a path next to the notebook).

    The notebook's widgets name the source catalog and where the tables go. A
    failed table fails the run, with the outcome in the error message.
    """
    arg = dbutils.widgets.get
    with open(library, encoding="utf-8") as f:  # notebooks run in their own folder
        code = f.read()
    spec: dict[str, Any] = {
        "source_catalog": arg("source_catalog"),
        "write": {"catalog": arg("catalog"), "schema": arg("schema")},
    }
    if library.endswith(".sql"):
        spec |= {"language": "sql", "targets": parse_sql(code)}
    else:
        spec |= {"language": "pyspark", "code": code}
    outcome = main(spark, spec)
    if outcome["errors"] or outcome["write_errors"]:
        raise RuntimeError(MARKER + json.dumps(outcome))
    dbutils.notebook.exit(MARKER + json.dumps(outcome))
