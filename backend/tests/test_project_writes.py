"""Runs bound to a project reading and writing on Databricks (docs/api/data-nodes.md,
"生成的代码怎么读源表和参数", "写到哪里，只写本项目的表", "写表方式"): the code's
preamble, the checks on param() and filtered sources, the bundle's project settings, the
cluster program's write modes and ownership rules (on a fake Spark), and where the dev
run deploys and runs."""

import json
from typing import Any

import pytest
import yaml

from aep import cluster_harness
from aep.databricks import Placement
from aep.etl import codegen
from aep.etl.plan import EtlPlan, PlanError, validate_plan
from aep.runtime.flow.nodes import TYPES as DATA_TYPES
from aep.runtime.flow.nodes import NodeFailure, Runtime
from tests import fake_databricks
from tests.conftest import FakeGateway
from tests.test_etl import plan

FORECAST = "prev.sales.Forecast"
PROJECT: dict[str, Any] = {
    "project_id": "jade-ibf",
    "name": "IBF 预测",
    "description": "",
    "table_prefix": "ibf_",
    "environment": {
        "key": "dev",
        "label": "开发",
        "catalog": "icsd-ace-dev",
        "schema": "jade_dev",
        "cluster_id": fake_databricks.CLUSTER_ID,
    },
    "references": [
        {"name": "PREV.sales.forecast", "label": "预测表", "filter": "Type = 'Vol.'"},
        {"name": "prev.sales.sku", "label": "物料", "filter": None},
    ],
    "adopted": ["ibf_table1_cw"],
    "params": [
        {"key": "update_month", "label": "统计月", "type": "month"},
        {"key": "cutoff", "label": "截止日", "type": "date"},
        {"key": "scale", "label": "系数", "type": "number"},
    ],
    "values": {"update_month": "2026-09", "cutoff": "2026-10-01", "scale": 1.5},
    "policy": {"samples": None, "models": None},
}


def lake_plan(language: str = "pyspark", **load: Any) -> EtlPlan:
    raw = plan(language=language)
    target = raw["targets"][0]
    target |= {
        "name": "ibf_table2",
        "sources": [FORECAST, "prev.sales.sku"],
        "load": load or {"mode": "full"},
    }
    return EtlPlan.model_validate(raw)


BODY = """
rows = source("prev.sales.Forecast").where(F.col("month") == param("update_month"))
return rows.join(source("prev.sales.sku"), "sku")
"""


# The code


class Table:
    def __init__(self, name: str) -> None:
        self.name = name
        self.filters: list[str] = []

    def where(self, condition: str) -> "Table":
        self.filters.append(condition)
        return self


def preamble(pipeline: str, conf: dict[str, str]) -> dict[str, Any]:
    """The platform's part of a pipeline file, run with a stand-in for spark."""
    read: list[Table] = []

    class Spark:
        class conf:  # noqa: N801
            @staticmethod
            def get(key: str, default: str | None = None) -> str | None:
                return conf.get(key, default)

        class read:  # noqa: N801
            @staticmethod
            def table(name: str) -> Table:
                read.append(Table(name))
                return read[-1]

    head = pipeline[: pipeline.index("\n\n\n#")]
    namespace: dict[str, Any] = {"spark": Spark, "read": read}
    exec(head.replace("from pyspark", "# from pyspark"), namespace)
    return namespace


def test_sources_by_two_or_three_parts_with_the_projects_filters():
    confirmed = lake_plan()
    pipeline = codegen.render_pyspark(
        confirmed, {"ibf_table2": BODY}, codegen.source_filters(confirmed, PROJECT)
    )
    # Reference names are not case sensitive; the code uses the plan's spelling.
    assert codegen.source_filters(confirmed, PROJECT) == {FORECAST: "Type = 'Vol.'"}
    params = json.dumps(PROJECT["values"])
    ns = preamble(pipeline, {"source_catalog": "lake", "aep_params": params})

    forecast = ns["source"](FORECAST)
    assert (forecast.name, forecast.filters) == ("`prev`.`sales`.`Forecast`", ["Type = 'Vol.'"])
    sku = ns["source"]("prev.sales.sku")
    assert (sku.name, sku.filters) == ("`prev`.`sales`.`sku`", [])
    assert ns["source"]("sales.orders").name == "`lake`.`sales`.`orders`"
    assert ns["param"]("update_month") == "2026-09" and ns["param"]("scale") == 1.5
    assert ns["param"]("missing") is None

    # Without a project: no filters, no parameters.
    bare = codegen.render_pyspark(confirmed, {"ibf_table2": BODY})
    assert "SOURCE_FILTERS = {}" in bare
    assert preamble(bare, {"source_catalog": "lake"})["param"]("update_month") is None


@pytest.mark.parametrize(
    "body, project, problem",
    [
        (BODY, None, "ibf_table2: 没有绑定项目，不能用 param()"),
        (
            BODY.replace('param("update_month")', "param(key)"),
            PROJECT,
            "param() 的参数要写成字符串",
        ),
        (BODY.replace("update_month", "month"), PROJECT, "param('month') 不是项目定义的参数"),
        ("return PARAMS", PROJECT, "不要直接用 PARAMS"),
        ("SOURCE_FILTERS.clear()\nreturn source('prev.sales.sku')", PROJECT, "不要直接用"),
        ("source = spark.read.table\nreturn source('x')", PROJECT, "不要重新定义平台提供的 source"),
        ("def param(n):\n    return 1\nreturn source('prev.sales.sku')", PROJECT, "重新定义"),
        ("global F\nreturn source('prev.sales.sku')", PROJECT, "不要写 global 或 nonlocal"),
        (
            BODY.replace("prev.sales.Forecast", "prev.sales.forecast"),
            PROJECT,
            "不在方案列出的来源里",
        ),
    ],
)
def test_param_and_source_checks(body, project, problem):
    problems = codegen.check_code(lake_plan(), {"ibf_table2": body}, project)
    assert any(problem in p for p in problems), problems


def test_good_code_passes_and_json_may_be_read():
    assert codegen.check_code(lake_plan(), {"ibf_table2": BODY}, PROJECT) == []
    reads = 'n = json.loads("1")\nreturn source("prev.sales.sku").limit(n)'
    assert codegen.check_code(lake_plan(), {"ibf_table2": reads}, PROJECT) == []


SKU = 'return source("prev.sales.sku")'


@pytest.mark.parametrize(
    "body, problem",
    [
        (f"import os\n{SKU}", "不能 import os，能 import 的只有 re、math"),
        (f"import re, subprocess\n{SKU}", "不能 import subprocess"),
        (f"from os import path\n{SKU}", "不能从 os import"),
        (f"from . import helpers\n{SKU}", "不能从 . import"),
        (f"from pyspark.sql import SparkSession\n{SKU}", "不能从 pyspark.sql import"),
        (f"import pyspark.sql.types\n{SKU}", "要写成 import pyspark.sql.types as 别名"),
        (f"from re import *\n{SKU}", "不要写 import *"),
        (f"from math import floor as param\n{SKU}", "不要重新定义平台提供的 param"),
        (f"import pyspark.sql.window as F\n{SKU}", "不要重新定义平台提供的 F"),
        (
            'return source("prev.sales.sku").where(F.col("m") == update_month)',
            "用了没有定义的名字 update_month（能 import 的只有",
        ),
        (f"x = re.sub('a', 'b', 'c')\n{SKU}", "用了没有定义的名字 re"),
        (f"WARNINGS.append('x')\n{SKU}", "不要直接用 WARNINGS，告警用 warn()"),
        (f"warn = print\n{SKU}", "不要重新定义平台提供的 warn"),
    ],
)
def test_imports_and_undefined_names(body, problem):
    problems = codegen.check_code(lake_plan(), {"ibf_table2": body}, PROJECT)
    assert any(problem in p for p in problems), problems


def test_a_few_safe_imports_and_warnings_pass():
    body = """
import re
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from calendar import monthrange
import math
from pyspark.sql import functions as F, types
from pyspark.sql.window import Window
import pyspark.sql.types as T

month = param("update_month")
if not re.fullmatch(r"\\d{4}-\\d{2}", month or ""):
    raise ValueError(f"统计月格式不对：{month}")
year, mm = (int(p) for p in month.split("-"))
days = monthrange(year, mm)[1]
start = dt.date(year, mm, 1)
try:
    factor = Decimal(str(param("scale"))).quantize(Decimal("0.01"), ROUND_HALF_UP)
except ArithmeticError as exc:
    warn(f"系数不对，按 1 算：{exc}")
    factor = Decimal(1)


def weight(n):
    return math.floor(n * float(factor))


rows = [r for r in range(days) if r]
match month:
    case str() as text:
        warn(f"统计月 {text}，从 {start} 起")
w = Window.partitionBy("sku")
return (
    source("prev.sales.sku")
    .withColumn("d", F.lit(len(rows)).cast(T.IntegerType()))
    .withColumn("n", F.row_number().over(w))
    .withColumn("t", F.lit(weight(2)).cast(types.LongType()))
)
"""
    assert codegen.check_code(lake_plan(), {"ibf_table2": body}, PROJECT) == []


def test_warn_collects_what_the_code_reports():
    pipeline = codegen.render_pyspark(lake_plan(), {"ibf_table2": BODY})
    namespace = preamble(pipeline, {"source_catalog": "lake"})
    namespace["warn"]("Table 1 没有 2026-09 的数据")
    namespace["warn"](3)
    assert namespace["WARNINGS"] == ["Table 1 没有 2026-09 的数据", "3"]


def test_sql_names_parameters_and_filtered_references():
    raw = plan(language="sql")
    raw["targets"][0] |= {"name": "ibf_sku", "sources": ["icsd-ace-dev.sales.orders", "sales.x"]}
    confirmed = EtlPlan.model_validate(raw)
    query = (
        "SELECT * FROM icsd-ace-dev.sales.orders o JOIN sales.x x USING (id) "
        "WHERE o.month = ${param.update_month}"
    )
    rendered = codegen.render_sql(confirmed, {"ibf_sku": query})
    assert "FROM `icsd-ace-dev`.sales.orders o JOIN `${source_catalog}`.sales.x x" in rendered
    assert "${param.update_month}" in rendered  # replaced on the cluster, at run time
    assert codegen.check_code(confirmed, {"ibf_sku": query}, PROJECT) == []
    assert codegen.check_code(confirmed, {"ibf_sku": query}, None) == [
        "ibf_sku: 没有绑定项目，不能用 ${param.update_month}"
    ]
    other = query.replace("update_month", "nope")
    assert codegen.check_code(confirmed, {"ibf_sku": other}, PROJECT) == [
        "ibf_sku: ${param.nope} 不是项目定义的参数"
    ]
    filtered = codegen.check_code(lake_plan("sql"), {"ibf_table2": "SELECT 1"}, PROJECT)
    assert filtered == ["表 prev.sales.Forecast 有读取筛选，SQL 代码暂不支持，请改用 PySpark"]


def test_load_modes_wait_for_plan_version_three():
    for load in [{"mode": "merge", "keys": ["order_id"]}, {"mode": "append"}]:
        raw = plan()
        raw["targets"][0]["load"] = load
        with pytest.raises(PlanError) as failed:
            validate_plan(raw, ["sales.orders", "sales.customers"])
        assert "order_facts 的 load.mode 只能是 full、incremental" in failed.value.problems
    # Versions 1 and 2 overwrite; merge falls back to the primary key.
    assert lake_plan().targets[0].load.stored(["sku"]) == {
        "mode": "overwrite",
        "where": None,
        "keys": ["sku"],
    }
    assert lake_plan(mode="merge").targets[0].load.stored(["sku"])["keys"] == ["sku"]


def test_run_settings_carry_write_modes_and_parameter_types():
    assert codegen.run_settings(EtlPlan.model_validate(plan()), None) == {}
    where = "update_month = ${param.update_month}"
    settings = codegen.run_settings(lake_plan(mode="replace_where", where=where), PROJECT)
    assert settings == {
        "loads": {"ibf_table2": {"mode": "replace_where", "where": where, "keys": ["order_id"]}},
        "param_types": {"update_month": "month", "cutoff": "date", "scale": "number"},
        "source_catalog": False,
    }
    unbound = codegen.run_settings(lake_plan(mode="append"), None)
    assert unbound["loads"]["ibf_table2"]["mode"] == "append"
    assert unbound["param_types"] == {}


def test_a_bound_job_bundle_writes_to_the_environment_with_the_project():
    confirmed = lake_plan(mode="merge", keys=["sku"])
    bundle = codegen.render("ibf_table2", confirmed, {"ibf_table2": BODY}, "数据湖", job=True,
                            project=PROJECT)  # fmt: skip
    config = yaml.safe_load(bundle.files["databricks.yml"])
    assert config["targets"]["dev"]["variables"] == {
        "catalog": "icsd-ace-dev",
        "schema": "jade_dev",
        "cluster_id": fake_databricks.CLUSTER_ID,
    }
    variables = config["variables"]
    assert variables["project"]["default"] == "jade-ibf"
    assert variables["table_prefix"]["default"] == "ibf_"
    assert json.loads(variables["adopted"]["default"]) == ["ibf_table1_cw"]
    assert json.loads(variables["params"]["default"]) == PROJECT["values"]
    parameters = config["resources"]["jobs"]["ibf_table2"]["tasks"][0]["notebook_task"][
        "base_parameters"
    ]
    assert parameters["project"] == "${var.project}"
    assert parameters["run_id"] == "{{job.run_id}}"
    assert config["targets"]["prod"]["mode"] == "production"  # prod unchanged
    notebook = bundle.files[codegen.JOB_NOTEBOOK]
    assert f"{bundle.settings!r})" in notebook
    compile(notebook.replace("# Databricks notebook source", ""), "run.py", "exec")
    assert bundle.settings["loads"]["ibf_table2"] == {"mode": "merge", "where": None,
                                                      "keys": ["sku"]}  # fmt: skip
    assert "只写以 `ibf_` 开头" in bundle.files["README.md"]
    assert "按方案的写表方式写入目标表" in bundle.files["README.md"]
    assert (
        'SOURCE_FILTERS = {"prev.sales.Forecast": "Type = \'Vol.\'"}'
        in bundle.files[codegen.PIPELINE_PY]
    )

    # An unbound job bundle stays as it was.
    plain = codegen.render("shop_orders", EtlPlan.model_validate(plan()), {"order_facts": "x"},
                           "示例库", job=True)  # fmt: skip
    assert plain.settings == {}
    assert plain.files[codegen.JOB_NOTEBOOK].endswith(
        'run_notebook(spark, dbutils, "pipeline.py")\n'
    )
    assert "project" not in yaml.safe_load(plain.files["databricks.yml"])["variables"]


# The cluster program's writes, on a fake Spark


class Rows:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    def filter(self, condition: str) -> "Rows":
        return self


class Writer:
    def __init__(self, spark: "Spark") -> None:
        self.spark = spark
        self.how: str | None = None
        self.options: dict[str, str] = {}

    def mode(self, how: str) -> "Writer":
        self.how = how
        return self

    def option(self, key: str, value: str) -> "Writer":
        self.options[key] = value
        return self

    def saveAsTable(self, name: str) -> None:  # noqa: N802
        if self.spark.failing:
            raise RuntimeError(
                "[DELTA_FAILED_TO_MERGE_FIELDS] Failed to merge fields 'qty'\nat ..."
            )
        self.spark.log.append(("save", name, self.how, self.options))
        self.spark.tables.setdefault(name, {"props": {}, "columns": ["sku", "qty"]})


class Field:
    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.dataType = type("T", (), {"simpleString": lambda _: kind})()  # noqa: N815


FRAME_TYPES = {"sku": "string", "qty": "int", "note": "string"}


class Frame:
    def __init__(self, spark: "Spark", outside: int = 0, repeated: int = 0) -> None:
        self.spark = spark
        self.outside = outside
        self.repeated = repeated
        fields = [Field(name, kind) for name, kind in FRAME_TYPES.items()]
        self.schema = type("S", (), {"fields": fields})()

    @property
    def write(self) -> Writer:
        return Writer(self.spark)

    def filter(self, condition: str) -> Rows:
        self.spark.log.append(("filter", condition))
        return Rows(self.outside)

    def groupBy(self, *keys: str) -> Any:  # noqa: N802
        self.spark.log.append(("group", keys))
        repeated = Rows(self.repeated)
        return type("G", (), {"count": lambda _: repeated})()

    def createOrReplaceTempView(self, name: str) -> None:  # noqa: N802
        self.spark.log.append(("view", name))


class Spark:
    def __init__(self, tables: dict[str, dict[str, Any]] | None = None) -> None:
        self.tables = tables or {}
        self.failing = False
        self.log: list[tuple[Any, ...]] = []
        self.history: dict[str, Any] = {
            "version": 12,
            "operation": "WRITE",
            "operationMetrics": {"numOutputRows": "1200", "numDeletedRows": "340"},
        }
        spark = self

        class catalog:  # noqa: N801
            @staticmethod
            def tableExists(name: str) -> bool:  # noqa: N802
                return name in spark.tables

            @staticmethod
            def dropTempView(name: str) -> None:  # noqa: N802
                spark.log.append(("drop view", name))

        class read:  # noqa: N801
            @staticmethod
            def table(name: str) -> Any:
                # A table's columns have the result's types unless the test gives others.
                found = spark.tables[name]
                kinds = FRAME_TYPES | found.get("types", {})
                fields = [Field(c, kinds.get(c.lower(), "string")) for c in found["columns"]]
                schema = type("S", (), {"fields": fields})()
                return type("R", (), {"columns": found["columns"], "schema": schema})()

        self.catalog = catalog
        self.read = read

    def sql(self, text: str) -> Any:
        self.log.append(("sql", text))
        if text.startswith("SHOW TBLPROPERTIES"):
            name = text.removeprefix("SHOW TBLPROPERTIES ")
            props = self.tables[name]["props"]
            found = [{"key": k, "value": v} for k, v in props.items()]
        elif text.startswith("DESCRIBE HISTORY"):
            found = [self.history]
        else:
            found = []
        return type("Q", (), {"collect": lambda _: found})()


STORED = "`icsd-ace-dev`.`jade_dev`.`ibf_table2`"
BOUND = {
    "write": {"catalog": "icsd-ace-dev", "schema": "jade_dev"},
    "project": {
        "id": "jade-ibf",
        "name": "IBF 预测",
        "table_prefix": "ibf_",
        "adopted": ["ibf_table1_cw"],
    },  # fmt: skip
    "params": {"update_month": "2026-09"},
    "param_types": {"update_month": "month"},
    "run_id": "run-7",
}


def store(spark: Spark, name: str = "ibf_table2", frame: Frame | None = None, **load: Any):
    spec = BOUND | {"loads": {name: load}} if load else BOUND
    return cluster_harness._store(spark, frame or Frame(spark), name, spec)


def sqls(spark: Spark) -> list[str]:
    return [entry[1] for entry in spark.log if entry[0] == "sql"]


def test_parameters_become_sql_literals():
    literal = cluster_harness.sql_literal
    assert literal("it's \\ ok") == "'it\\'s \\\\ ok'"
    assert literal("2026-09", "month") == "'2026-09'"
    assert literal("2026-10-01", "date") == "DATE'2026-10-01'"
    assert (literal(3, "number"), literal(1.5), literal(True), literal(None)) == (
        "3",
        "1.5",
        "true",
        "NULL",
    )
    text = "month = ${param.update_month} AND d < ${param.cutoff}"
    values = {"update_month": "2026-09", "cutoff": "2026-10-01"}
    kinds = {"update_month": "month", "cutoff": "date"}
    assert cluster_harness.with_params(text, values, kinds) == (
        "month = '2026-09' AND d < DATE'2026-10-01'"
    )
    with pytest.raises(ValueError, match="没有运行参数 nope"):
        cluster_harness.with_params("${param.nope}", values, kinds)


def test_a_quoted_parameter_is_the_value_as_text():
    values = {"version_id": "FY26_06_M", "n": 3, "cutoff": "2026-10-01", "gone": None, "q": "it's"}
    kinds = {"version_id": "text", "n": "number", "cutoff": "date", "gone": "text", "q": "text"}

    def swap(text: str) -> str:
        return cluster_harness.with_params(text, values, kinds)

    # Written either way, the condition compares with the same literal.
    assert swap("version_id = '${param.version_id}'") == "version_id = 'FY26_06_M'"
    assert swap("version_id = ${param.version_id}") == "version_id = 'FY26_06_M'"
    assert swap("label = '${param.q}'") == swap("label = ${param.q}") == "label = 'it\\'s'"
    # Quoted, a number or date is text; bare, it keeps its type.
    assert swap("'${param.n}'") == "'3'"
    assert swap("${param.n}") == "3"
    assert swap("d = '${param.cutoff}'") == "d = '2026-10-01'"
    assert swap("d = ${param.cutoff}") == "d = DATE'2026-10-01'"
    # Quoted NULL stays NULL, not the text 'None'.
    assert swap("'${param.gone}'") == "NULL"
    # Only quotes around the whole parameter count.
    assert swap("x = 'a' AND y = ${param.n}") == "x = 'a' AND y = 3"


def test_a_new_table_is_created_marked_and_its_history_read():
    spark = Spark()
    found = store(spark)
    assert spark.log[0] == ("save", STORED, "overwrite", {"overwriteSchema": "true"})
    assert found == {
        "mode": "overwrite",
        "version": 12,
        "operation": "WRITE",
        "rows_written": 1200,
        "rows_updated": None,
        "rows_deleted": 340,
    }
    # The history is read before the marks, which would be the latest entry.
    assert sqls(spark) == [
        f"DESCRIBE HISTORY {STORED} LIMIT 1",
        f"ALTER TABLE {STORED} SET TBLPROPERTIES ('aep.project' = 'jade-ibf', "
        "'aep.run_id' = 'run-7')",
    ]


@pytest.mark.parametrize(
    "props, name, written",
    [
        ({"aep.project": "jade-ibf"}, "ibf_table2", True),
        ({}, "ibf_table1_cw", True),  # adopted
        ({}, "ibf_table2", False),
        ({"aep.project": "other"}, "ibf_table2", False),
    ],
)
def test_only_the_projects_tables_are_written(props, name, written):
    stored = f"`icsd-ace-dev`.`jade_dev`.`{name}`"
    spark = Spark({stored: {"props": props, "columns": ["sku"]}})
    if written:
        store(spark, name)
        assert spark.log[1][0] == "save"
        return
    with pytest.raises(cluster_harness._Refused) as refused:
        store(spark, name)
    why = "项目标记是另一个项目 other" if props else "没有项目标记，也没有登记在“已有的表”里"
    assert str(refused.value) == (
        f"表 icsd-ace-dev.jade_dev.{name} 已经存在，不属于项目「IBF 预测」（{why}），没有写"
    )
    assert not [entry for entry in spark.log if entry[0] == "save"]


def test_tables_without_the_prefix_and_unbound_runs():
    with pytest.raises(cluster_harness._Refused, match="不以项目的表名前缀 ibf_ 开头"):
        store(Spark(), "table2")
    # Unbound: any table, no marks, no ownership check.
    spark = Spark({"`c`.`s`.`t`": {"props": {}, "columns": []}})
    spec = {"write": {"catalog": "c", "schema": "s"}}
    cluster_harness._store(spark, Frame(spark), "t", spec)
    assert sqls(spark) == ["DESCRIBE HISTORY `c`.`s`.`t` LIMIT 1"]


MINE = {"aep.project": "jade-ibf"}
SAME = ["sku", "qty", "note"]  # the columns of the result (Frame)


def test_rows_written_back_from_rewritten_files_are_not_counted_as_written():
    spark = Spark({STORED: {"props": MINE, "columns": SAME}})
    spark.history["operationMetrics"] = {
        "numOutputRows": "12",
        "numDeletedRows": "1",
        "numCopiedRows": "2",
    }
    found = store(spark, mode="replace_where", where="update_month = '2026-10'")
    assert (found["rows_written"], found["rows_deleted"], found["rows_copied"]) == (10, 1, 2)


def test_replace_where():
    where = "update_month = ${param.update_month}"
    spark = Spark({STORED: {"props": MINE, "columns": SAME}})
    store(spark, mode="replace_where", where=where, keys=[])
    condition = "update_month = '2026-09'"
    assert ("filter", f"NOT coalesce(({condition}), false)") in spark.log
    [save] = [entry for entry in spark.log if entry[0] == "save"]
    # The project's table keeps its columns: no schema merging.
    assert save == ("save", STORED, "overwrite", {"replaceWhere": condition})

    spark = Spark()
    with pytest.raises(cluster_harness._Refused) as refused:
        store(spark, frame=Frame(spark, outside=3), mode="replace_where", where=where, keys=[])
    assert str(refused.value) == f"ibf_table2: 有 3 行不满足替换条件 {condition}，没有写"
    # A new table is created by the write itself.
    spark = Spark()
    store(spark, mode="replace_where", where=where, keys=[])
    assert [e for e in spark.log if e[0] == "save"][0][2:] == ("overwrite", {})


def test_append_and_merge():
    spark = Spark({STORED: {"props": MINE, "columns": SAME}})
    store(spark, mode="append", where=None, keys=[])
    assert ("save", STORED, "append", {}) in spark.log

    spark = Spark({STORED: {"props": MINE, "columns": ["SKU", "qty"]}})
    spark.history = {
        "version": 3,
        "operation": "MERGE",
        "operationMetrics": {
            "numTargetRowsInserted": "5",
            "numTargetRowsUpdated": "7",
            "numTargetRowsDeleted": "0",
            "numOutputRows": "12",
        },
    }
    note = [{"name": "note", "after": "qty"}]
    found = store(spark, mode="merge", where=None, keys=["sku"], add_columns=note)
    assert sqls(spark)[1:3] == [
        f"ALTER TABLE {STORED} ADD COLUMNS (`note` string AFTER `qty`)",
        f"MERGE INTO {STORED} AS t USING aep_merge_ibf_table2 AS s ON t.`sku` <=> s.`sku` "
        "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *",
    ]
    assert ("drop view", "aep_merge_ibf_table2") in spark.log
    assert found == {
        "mode": "merge",
        "version": 3,
        "operation": "MERGE",
        "rows_written": 5,
        "rows_updated": 7,
        "rows_deleted": 0,
        "columns_added": [{"name": "note", "type": "string", "after": "qty"}],
    }

    spark = Spark()
    with pytest.raises(cluster_harness._Refused) as refused:
        store(spark, frame=Frame(spark, repeated=3), mode="merge", where=None, keys=["sku", "m"])
    assert str(refused.value) == "ibf_table2: 合并键 sku、m 有 3 组重复，没有写"
    spark = Spark()
    store(spark, mode="merge", where=None, keys=["sku"])  # a new table: created
    assert [e for e in spark.log if e[0] == "save"][0][2] is None


def test_an_existing_tables_columns_change_only_as_the_plan_confirmed():
    where = "update_month = ${param.update_month}"
    condition = "update_month = '2026-09'"

    def refused(columns: list[str], **load: Any) -> str:
        spark = Spark({STORED: {"props": MINE, "columns": columns}})
        with pytest.raises(cluster_harness._Refused) as failed:
            store(spark, mode="replace_where", where=where, keys=[], **load)
        assert not [e for e in spark.log if e[0] == "save" or "ADD COLUMNS" in str(e)]
        return str(failed.value)

    # A new column the plan did not confirm, a column the result lacks, a place not there.
    assert refused(["sku", "note"]) == (
        "ibf_table2: 结果比表多了列 qty，确认的方案里没有要加这些列，没有写"
    )
    assert refused([*SAME, "Region"]) == "ibf_table2: 结果里没有表里已有的列 Region，没有写"
    assert refused(["sku", "note"], add_columns=[{"name": "qty", "after": "nope"}]) == (
        "ibf_table2: 方案要把列 qty 加在 nope 后面，表里没有列 nope，没有写"
    )

    # Confirmed: added where the plan says (names are not case sensitive), then written
    # without merging the schema; the result says what was added.
    spark = Spark({STORED: {"props": MINE, "columns": ["SKU", "note"]}})
    found = store(
        spark,
        mode="replace_where",
        where=where,
        keys=[],
        add_columns=[{"name": "QTY", "after": "sku"}],
    )
    assert sqls(spark)[1] == f"ALTER TABLE {STORED} ADD COLUMNS (`qty` int AFTER `SKU`)"
    assert ("save", STORED, "overwrite", {"replaceWhere": condition}) in spark.log
    assert found["columns_added"] == [{"name": "qty", "type": "int", "after": "sku"}]

    # First, or one after another; a column the table already has is left alone.
    spark = Spark({STORED: {"props": MINE, "columns": ["note"]}})
    added = [{"name": "sku", "after": None}, {"name": "qty", "after": "sku"}]
    store(spark, mode="append", where=None, keys=[], add_columns=added)
    assert [q for q in sqls(spark) if "ADD COLUMNS" in q] == [
        f"ALTER TABLE {STORED} ADD COLUMNS (`sku` string FIRST)",
        f"ALTER TABLE {STORED} ADD COLUMNS (`qty` int AFTER `sku`)",
    ]
    spark = Spark({STORED: {"props": MINE, "columns": SAME}})
    found = store(spark, mode="append", where=None, keys=[], add_columns=added)
    assert not [q for q in sqls(spark) if "ADD COLUMNS" in q] and "columns_added" not in found

    # A write that fails after the columns were added says so.
    spark = Spark({STORED: {"props": MINE, "columns": ["sku", "note"]}})
    spark.failing = True
    with pytest.raises(cluster_harness._Refused) as failed:
        store(
            spark, mode="append", where=None, keys=[], add_columns=[{"name": "qty", "after": "sku"}]
        )
    assert str(failed.value) == (
        "ibf_table2: RuntimeError: [DELTA_FAILED_TO_MERGE_FIELDS] Failed to merge fields 'qty'"
        "（列 qty 已经加到表里，数据没有写）"
    )

    # Overwriting rebuilds the table; a new table is created as the result is.
    spark = Spark({STORED: {"props": MINE, "columns": ["other"]}})
    store(spark)
    assert spark.log[1] == ("save", STORED, "overwrite", {"overwriteSchema": "true"})
    # Unbound runs merge the schema as before.
    spark = Spark({"`c`.`s`.`t`": {"props": {}, "columns": ["sku"]}})
    spec = {"write": {"catalog": "c", "schema": "s"}, "loads": {"t": {"mode": "append"}}}
    cluster_harness._store(spark, Frame(spark), "t", spec)
    assert ("save", "`c`.`s`.`t`", "append", {"mergeSchema": "true"}) in spark.log


@pytest.mark.parametrize(
    ("written", "spark"),
    [
        ("STRING", "string"),
        ("Integer", "int"),
        ("long", "bigint"),
        ("DECIMAL", "decimal(10,0)"),
        ("decimal(18, 2)", "decimal(18,2)"),
        ("NUMERIC(5)", "decimal(5,0)"),
        ("VARCHAR(10)", "string"),
        ("ARRAY< INT >", "array<int>"),
        ("map<string,bigint>", "map<string,bigint>"),
        ("struct<a:int,b:array<string>>", "struct<a:int,b:array<string>>"),
        ("timestamp_ntz", "timestamp_ntz"),
    ],
)
def test_a_type_is_written_the_way_spark_writes_it(written: str, spark: str):
    assert cluster_harness.spark_type(written) == spark


@pytest.mark.parametrize(
    "written",
    ["", "  ", "text column", "foo", "decimal(40,2)", "decimal(5,6)", "array<foo>",
     "decimal(18,2", "char", "map<string>", None, 3],
)  # fmt: skip
def test_anything_else_is_no_type(written: Any):
    assert cluster_harness.spark_type(written) is None


def test_columns_keep_their_kind_and_an_added_column_the_confirmed_type():
    where = "update_month = ${param.update_month}"

    def refused(columns: list[str], types: dict[str, str] | None = None, **load: Any) -> str:
        spark = Spark({STORED: {"props": MINE, "columns": columns, "types": types or {}}})
        with pytest.raises(cluster_harness._Refused) as failed:
            store(spark, mode="replace_where", where=where, keys=[], **load)
        assert not [e for e in spark.log if e[0] == "save" or "ADD COLUMNS" in str(e)]
        return str(failed.value)

    # The result's qty is an int: as text, or a list, it is not the table's column.
    assert refused(SAME, {"qty": "string", "note": "array<string>"}) == (
        "ibf_table2: 列的类型和表里的不一致（qty 在表里是 STRING，结果里是 INT；"
        "note 在表里是 ARRAY<STRING>，结果里是 STRING），没有写"
    )
    # Another width of number is left to Delta.
    spark = Spark({STORED: {"props": MINE, "columns": SAME, "types": {"qty": "bigint"}}})
    store(spark, mode="replace_where", where=where, keys=[])
    assert [e for e in spark.log if e[0] == "save"]

    # An added column must come out as the plan confirmed it: nothing is added otherwise.
    confirmed = [{"name": "qty", "after": "sku", "type": "bigint"}]
    assert refused(["sku", "note"], add_columns=confirmed) == (
        "ibf_table2: 确认的方案里新加的列 qty 是 BIGINT，结果里是 INT，没有加列，也没有写："
        "代码要让这一列是 BIGINT（需要时用 cast）"
    )
    spark = Spark({STORED: {"props": MINE, "columns": ["sku", "note"]}})
    found = store(
        spark,
        mode="replace_where",
        where=where,
        keys=[],
        add_columns=[{"name": "qty", "after": "sku", "type": "INTEGER"}],
    )
    assert sqls(spark)[1] == f"ALTER TABLE {STORED} ADD COLUMNS (`qty` int AFTER `sku`)"
    assert found["columns_added"] == [{"name": "qty", "type": "int", "after": "sku"}]


def test_the_history_is_optional():
    spark = Spark()
    spark.history = None  # type: ignore[assignment]
    found = store(spark)
    assert found == {
        "mode": "overwrite",
        "version": None,
        "operation": None,
        "rows_written": None,
        "rows_updated": None,
        "rows_deleted": None,
    }


def test_the_notebook_reads_the_project_from_its_widgets(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    def main(spark: Any, spec: dict[str, Any]) -> dict[str, Any]:
        seen.update(spec)
        return {"tables": [], "errors": [], "write_errors": []}

    widgets = {
        "source_catalog": "",
        "catalog": "icsd-ace-dev",
        "schema": "jade_dev",
        "project": "jade-ibf",
        "project_name": "IBF 预测",
        "table_prefix": "ibf_",
        "adopted": '["ibf_table1_cw"]',
        "params": '{"update_month": "2026-09"}',
        "run_id": "run-7",
    }

    class Dbutils:
        class widgets:  # noqa: N801
            @staticmethod
            def get(name: str) -> str:
                return widgets[name]

        class notebook:  # noqa: N801
            @staticmethod
            def exit(text: str) -> None:
                seen["exit"] = text

    monkeypatch.setattr(cluster_harness, "main", main)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pipeline.py").write_text("x = 1\n")
    settings = {"loads": {"ibf_table2": {"mode": "append"}}, "param_types": {"m": "month"}}
    cluster_harness.run_notebook(None, Dbutils, "pipeline.py", settings)
    assert seen["project"] == {
        "id": "jade-ibf",
        "name": "IBF 预测",
        "table_prefix": "ibf_",
        "adopted": ["ibf_table1_cw"],
    }
    assert (seen["params"], seen["run_id"], seen["loads"]) == (
        {"update_month": "2026-09"},
        "run-7",
        settings["loads"],
    )

    # A job deployed before these widgets existed: unbound, as before.
    seen.clear()
    del widgets["project"], widgets["params"], widgets["run_id"]

    def missing(name: str) -> str:
        if name not in widgets:
            raise RuntimeError(f"InputWidgetNotDefined: {name}")
        return widgets[name]

    monkeypatch.setattr(Dbutils.widgets, "get", staticmethod(missing))
    cluster_harness.run_notebook(None, Dbutils, "pipeline.py")
    assert "project" not in seen and seen["params"] == {} and seen["loads"] == {}


# Where the dev run deploys and runs


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("aep.databricks.POLL_SECONDS", 0)
    monkeypatch.setattr("aep.databricks.CLUSTER_POLL_SECONDS", 0)


WRITE = {
    "mode": "merge",
    "version": 3,
    "operation": "MERGE",
    "rows_written": 5,
    "rows_updated": 7,
    "rows_deleted": 0,
}
PASSED = {
    "tables": [
        {
            "name": "ibf_table2",
            "status": "COMPLETED",
            "rows": 12,
            "dropped": 0,
            "expectations": [],
            "write": WRITE,
        }  # fmt: skip
    ]
}


def bound_bundle(job: bool = True) -> codegen.Bundle:
    confirmed = lake_plan(mode="merge", keys=["sku"])
    return codegen.render("ibf_table2", confirmed, {"ibf_table2": BODY}, "数据湖", job=job,
                          project=PROJECT)  # fmt: skip


async def test_a_bound_job_runs_in_the_environment_and_marks_the_run(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    # The platform's own cluster and catalog are not the environment's.
    target = workspace.target(
        databricks_cluster_id="9999-other", databricks_source_catalog="", databricks_deploy="job"
    )
    placement = Placement.of(PROJECT, "run-7")

    dev = await target.run(bound_bundle(), placement=placement)

    assert dev.status == "passed", dev.errors
    assert (dev.cluster_id, dev.catalog, dev.schema) == (
        fake_databricks.CLUSTER_ID,
        "icsd-ace-dev",
        "jade_dev",
    )
    assert dev.tables[0].write == WRITE
    [job] = workspace.jobs.values()
    task = job["tasks"][0]
    assert task["existing_cluster_id"] == fake_databricks.CLUSTER_ID
    parameters = task["notebook_task"]["base_parameters"]
    assert parameters | {"params": json.loads(parameters["params"])} == {
        "catalog": "icsd-ace-dev",
        "schema": "jade_dev",
        "source_catalog": "",  # every source is on Databricks already
        "project": "jade-ibf",
        "project_name": "IBF 预测",
        "table_prefix": "ibf_",
        "adopted": '["ibf_table1_cw"]',
        "params": PROJECT["values"],
        "run_id": "{{job.run_id}}",
    }
    [run_now] = [r for r in workspace.requests if r.url.path.endswith("/jobs/run-now")]
    assert json.loads(run_now.content)["notebook_params"] == {"run_id": "run-7"}


async def test_a_bound_run_on_the_cluster_gets_the_parameters(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_outputs = [{"tables": PASSED["tables"][:1]}]
    target = workspace.target(databricks_cluster_id="9999-other", databricks_deploy="cluster",
                              databricks_source_catalog="")  # fmt: skip

    dev = await target.run(bound_bundle(job=False), placement=Placement.of(PROJECT, "run-7"))

    assert dev.status == "passed", dev.errors
    assert dev.cluster_id == fake_databricks.CLUSTER_ID
    call = workspace.commands[0]["command"].rsplit("\n", 1)[1]
    spec = json.loads(json.loads(call.removeprefix("main(spark, json.loads(").removesuffix("))")))
    assert spec["params"] == PROJECT["values"]
    assert spec["param_types"]["update_month"] == "month"
    assert "write" not in spec and "project" not in spec  # nothing is written here


async def test_what_the_code_warns_goes_into_the_dev_run(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_outputs = [
        {"tables": PASSED["tables"][:1], "warnings": ["Table 1 没有 FY26_06_M 的数据"]}
    ]
    target = workspace.target(databricks_cluster_id="9999-other", databricks_deploy="cluster",
                              databricks_source_catalog="")  # fmt: skip

    dev = await target.run(bound_bundle(job=False), placement=Placement.of(PROJECT, "run-7"))

    assert dev.status == "passed", dev.errors
    assert "代码告警：Table 1 没有 FY26_06_M 的数据" in dev.warnings


def runtime(workspace: fake_databricks.FakeWorkspace, **settings: Any) -> Runtime:
    target = workspace.target(**settings)
    return Runtime(
        FakeGateway().client, "mock", {}, {}, databricks=target, settings=target.settings
    )


def dev_state(bundle: codegen.Bundle, project: dict[str, Any] | None = PROJECT) -> dict[str, Any]:
    found = {"name": bundle.name, "language": "pyspark", "files": bundle.files}
    if bundle.settings:
        found["settings"] = bundle.settings
    state: dict[str, Any] = {"vars": {"check_code": {"bundle": found}}, "run_id": "run-7"}
    if project is not None:
        state["project"] = project
    return state


DEV = {"id": "dev_run", "type": "databricks_dev_run", "bundle": "{{check_code.bundle}}",
       "preview_rows": 0}  # fmt: skip


async def test_the_dev_run_node_refuses_pipelines_and_tables_outside_the_prefix(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    run = DATA_TYPES["databricks_dev_run"].run
    rt = runtime(workspace, databricks_deploy="pipeline")
    with pytest.raises(NodeFailure) as failed:
        await run(DEV, dev_state(bound_bundle()), rt)
    assert (failed.value.type, failed.value.message) == (
        "input_error",
        "绑定项目的运行请用 job 或 cluster 方式（AEP_DATABRICKS_DEPLOY）",
    )
    other = dict(PROJECT, table_prefix="jade_")
    rt = runtime(
        workspace, databricks_deploy="job", databricks_cluster_id=fake_databricks.CLUSTER_ID
    )
    with pytest.raises(NodeFailure) as failed:
        await run(DEV, dev_state(bound_bundle(), other), rt)
    assert failed.value.message == "目标表 ibf_table2 不以项目「IBF 预测」的表名前缀 jade_ 开头"
    assert workspace.jobs == {}  # nothing was deployed


async def test_the_dev_run_node_writes_where_the_environment_says(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    rt = runtime(workspace, databricks_deploy="job", databricks_cluster_id="9999-other")
    out = await DATA_TYPES["databricks_dev_run"].run(DEV, dev_state(bound_bundle()), rt)
    assert out.branch == "passed"
    [table] = out.outputs["tables"]
    assert table["target"] == "`icsd-ace-dev`.jade_dev.ibf_table2"
    assert table["write"] == WRITE
    assert (out.outputs["run"]["catalog"], out.outputs["run"]["schema"]) == (
        "icsd-ace-dev",
        "jade_dev",
    )
    assert "| 写表 |" in out.outputs["report"]
    assert "按键合并，版本 3，写入 5 行，更新 7 行，删除 0 行" in out.outputs["report"]


def test_write_text():
    from aep.runtime.flow.data_nodes import write_text

    assert write_text(None) == "—"
    assert write_text({"mode": "append", "version": None, "rows_written": None}) == "追加"
    copied = {"mode": "replace_where", "version": 3, "rows_written": 10, "rows_deleted": 1}
    assert write_text(copied | {"rows_copied": 2}) == (
        "按条件替换，版本 3，写入 10 行，删除 1 行，"
        "另把同一文件里 2 行其他数据原样写回（Delta 重写文件）"
    )
    added = [
        {"name": "platform_original", "type": "string", "after": "platform"},
        {"name": "code", "type": "int", "after": None},
    ]
    assert write_text({"mode": "replace_where", "version": 4, "columns_added": added}) == (
        "按条件替换，版本 4，加列 platform_original（string，放在 platform 后面），"
        "加列 code（int，放在最前面）"
    )


async def test_the_code_check_node_binds_the_bundle_to_the_project():
    confirmed = lake_plan(mode="replace_where", where="update_month = ${param.update_month}")
    node = {
        "id": "check_code",
        "type": "etl_code_check",
        "reply": "{{codegen.text}}",
        "plan": "{{check_plan.plan}}",
    }
    state = {
        "vars": {
            "codegen": {"text": json.dumps({"targets": {"ibf_table2": BODY}})},
            "check_plan": {"plan": confirmed.model_dump()},
        },
        "project": PROJECT,
    }
    rt = Runtime(FakeGateway().client, "mock", {}, {})
    out = await DATA_TYPES["etl_code_check"].run(node, state, rt)
    assert out.outputs["ok"], out.outputs["problems"]
    bundle = out.outputs["bundle"]
    assert bundle["settings"]["loads"]["ibf_table2"]["mode"] == "replace_where"
    assert 'SOURCE_FILTERS = {"prev.sales.Forecast"' in bundle["files"][codegen.PIPELINE_PY]

    unbound = await DATA_TYPES["etl_code_check"].run(node, state | {"project": None}, rt)
    assert unbound.branch == "retry"
    assert "没有绑定项目，不能用 param()" in unbound.outputs["problems"]
