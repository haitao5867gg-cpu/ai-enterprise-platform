"""Runs a generated pipeline's transformations on an existing Databricks cluster.

This file runs on Databricks and imports nothing from ``aep``. It evaluates each
target table like a pipeline update would: PySpark code runs with a stand-in
for ``pyspark.pipelines`` that collects the tables and their quality rules, SQL
targets are parsed from the pipeline file. A table read by another target is the
transformed result with its drop rules applied, as it would be stored. The
outcome is printed as one ``AEP_RESULT`` line of JSON; with ``spec["preview"]`` it
also has the first rows of each table that passed.

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
        outcome["preview"] = _preview(done, spec["preview"])
    print(MARKER + json.dumps(outcome))
    return outcome


def _preview(done: dict[str, Any], rows: int) -> list[dict[str, Any]]:
    """The first ``rows`` rows of each table that passed, values as JSON writes them."""
    from pyspark.sql import functions as F

    out = []
    for name, df in done.items():
        if df is None:
            continue
        columns = list(df.columns)
        try:
            # Spark's own JSON writer converts the values (toJSON is missing under
            # Spark Connect, to_json is not).
            found = df.limit(rows).select(F.to_json(F.struct(*[df[c] for c in columns]))).collect()
        except Exception:  # noqa: BLE001 - a preview is a nicety, the outcome stands
            continue
        out.append(
            {
                "table": name,
                "columns": columns,
                "rows": [{c: json.loads(r[0]).get(c) for c in columns} for r in found],
            }
        )
    return out


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
