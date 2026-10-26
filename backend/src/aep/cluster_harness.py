"""Runs a generated pipeline's transformations on an existing Databricks cluster.

This file is sent as source to the cluster (``aep.databricks`` appends the call
to ``main``) and runs there; it imports nothing from ``aep``. It evaluates each
target table like a pipeline update would, without writing anything: PySpark
code runs with a stand-in for ``pyspark.pipelines`` that collects the tables and
their quality rules, SQL targets arrive already parsed. A table read by another
target is the transformed result with its drop rules applied, as it would be
stored. The outcome is printed as one ``AEP_RESULT`` line of JSON.
"""

import json
import re
import sys
import types
from typing import Any

MARKER = "AEP_RESULT "
KINDS = {"expect": "warn", "expect_or_drop": "drop", "expect_or_fail": "fail"}


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


def main(spark: Any, spec: dict[str, Any]) -> None:
    from pyspark.sql import functions as F

    defs: dict[str, tuple[Any, list[tuple[str, str, str]]]] = {}
    done: dict[str, Any] = {}
    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

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
        result["status"] = "COMPLETED"
        result["rows"] = total - result["dropped"]
        return df.filter(keep)

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

    print(MARKER + json.dumps({"tables": list(results.values()), "errors": errors}))
