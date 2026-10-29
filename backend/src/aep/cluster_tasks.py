"""Small programs the platform runs on an existing Databricks cluster for flow apps'
data-engineering nodes (aep.runtime.flow.data_nodes).

Like ``aep.cluster_harness`` this file runs on Databricks and imports nothing from
``aep``: ``aep.databricks`` sends it as source with a call appended. ``ingest``
loads source rows landed in a volume as JSON Lines into tables; ``preview`` reads
the first rows of tables. Each prints its outcome as one ``AEP_RESULT`` line of
JSON. What a table or column is called arrives as data, never as code, and goes
into SQL only quoted.
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


def _first_rows(df: Any, n: int) -> tuple[list[str], list[dict[str, Any]]]:
    """``df``'s columns and first ``n`` rows, converted by Spark's own JSON writer
    (``DataFrame.toJSON`` is missing under Spark Connect, ``to_json`` is not)."""
    from pyspark.sql import functions as F

    columns = list(df.columns)
    found = df.limit(n).select(F.to_json(F.struct(*[df[c] for c in columns]))).collect()
    return columns, [{c: json.loads(r[0]).get(c) for c in columns} for r in found]


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
    table), values as JSON writes them (dates as ISO text, decimals as numbers)."""
    tables: list[dict[str, Any]] = []
    errors: list[str] = []
    for target in spec["tables"]:
        try:
            df = spark.read.table(_quote(*target))
            columns, rows = _first_rows(df, spec["rows"])
        except Exception as exc:  # noqa: BLE001
            errors.append(_error(".".join(target), exc))
            continue
        tables.append({"target": target, "columns": columns, "rows": rows})
    outcome = {"tables": tables, "errors": errors}
    print(MARKER + json.dumps(outcome, ensure_ascii=False))
    return outcome
