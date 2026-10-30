"""Small programs the platform runs on an existing Databricks cluster for flow apps'
data-engineering nodes (aep.runtime.flow.data_nodes).

Like ``aep.cluster_harness`` this file runs on Databricks and imports nothing from
``aep``: ``aep.databricks`` sends it as source with a call appended. ``ingest``
loads source rows landed in a volume as JSON Lines into tables; ``preview`` reads
the first rows of tables and works out the answer a plan designs. Each prints its
outcome as one ``AEP_RESULT`` line of JSON. What a table or column is called arrives
as data, never as code, and goes into SQL only quoted.
"""

import json
from typing import Any

MARKER = "AEP_RESULT "


def _quote(*parts: str) -> str:
    return ".".join("`" + part.replace("`", "``") + "`" for part in parts)


def _literal(text: str) -> str:
    """A Spark SQL string literal."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _error(name: str, exc: BaseException) -> str:
    first = next((line for line in str(exc).splitlines() if line.strip()), "")
    return f"{name}: {type(exc).__name__}: {first.strip()[:1000]}"


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


def ingest(spark: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Replaces each of ``spec["tables"]`` with the rows landed at its ``path``.

    Every column is read as text and cast to its type (``columns``: name and Spark
    SQL type), so a value keeps what the source had rather than what reading JSON
    would guess. The table gets ``comment`` and ``properties``.
    """
    from pyspark.sql.types import StringType, StructField, StructType

    tables: list[dict[str, Any]] = []
    errors: list[str] = []
    for table in spec["tables"]:
        name = _quote(*table["target"])
        try:
            schema = StructType([StructField(c, StringType()) for c, _ in table["columns"]])
            spark.read.schema(schema).json(table["path"]).createOrReplaceTempView("aep_landed")
            casts = ", ".join(
                f"CAST({_quote(c)} AS {kind}) AS {_quote(c)}" for c, kind in table["columns"]
            )
            properties = ", ".join(
                f"{_literal(k)} = {_literal(v)}" for k, v in table["properties"].items()
            )
            spark.sql(
                f"CREATE OR REPLACE TABLE {name} COMMENT {_literal(table['comment'])} "
                f"TBLPROPERTIES ({properties}) AS SELECT {casts} FROM aep_landed"
            )
            rows = spark.read.table(name).count()
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            errors.append(_error(".".join(table["target"]), exc))
            continue
        tables.append({"target": table["target"], "rows": rows})
    outcome = {"tables": tables, "errors": errors}
    print(MARKER + json.dumps(outcome, ensure_ascii=False))
    return outcome


def preview(spark: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """The first ``spec["rows"]`` rows of each of ``spec["tables"]`` (catalog, schema,
    table), values as JSON writes them (dates as ``yyyy-MM-dd``, timestamps without a
    zone, decimals as numbers). With ``spec["order"]`` (table name to primary key) each
    table's rows are sorted by its key and say so in ``sorted_by``. With
    ``spec["answer"]`` (a design whose ``table`` is catalog, schema, table) the outcome
    also has the answer worked out on that table."""
    order = spec.get("order")
    tables: list[dict[str, Any]] = []
    errors: list[str] = []
    for target in spec["tables"]:
        try:
            df = spark.read.table(_quote(*target))
            keys = None if order is None else order.get(target[-1]) or []
            columns, rows, sorted_by = _first_rows(df, spec["rows"], keys)
        except Exception as exc:  # noqa: BLE001
            errors.append(_error(".".join(target), exc))
            continue
        found = {"target": target, "columns": columns, "rows": rows}
        tables.append(found if sorted_by is None else found | {"sorted_by": sorted_by})
    outcome: dict[str, Any] = {"tables": tables, "errors": errors}
    design = spec.get("answer")
    if design:
        try:
            df = spark.read.table(_quote(*design["table"]))
        except Exception as exc:  # noqa: BLE001 - the answer says why, the preview stands
            outcome["answer"] = {"error": _error(".".join(design["table"]), exc)}
        else:
            outcome["answer"] = _answer(df, design)
    print(MARKER + json.dumps(outcome, ensure_ascii=False))
    return outcome
