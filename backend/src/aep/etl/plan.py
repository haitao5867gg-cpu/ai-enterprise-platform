"""The ETL plan: what the model proposes and the user confirms before any code exists.

The model answers in JSON matching ``EtlPlan``; ``validate_plan`` then checks it
against the profiled source tables, so a plan that names a table or column that
does not exist goes back to the model instead of on to the user.
"""

import keyword
import re
from collections.abc import Collection
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Names the generated pipeline code already uses (see aep.etl.codegen).
RESERVED = {"source", "spark", "dp", "f", "window", "source_catalog"}

Language = Literal["pyspark", "sql"]


class TargetColumn(BaseModel):
    name: str
    source: str = Field(description="Spark SQL expression over the source columns")
    description: str = ""


class QualityRule(BaseModel):
    name: str
    condition: str = Field(description="Spark SQL boolean expression a valid row satisfies")
    on_violation: Literal["warn", "drop", "fail"] = "warn"


class Load(BaseModel):
    mode: Literal["full", "incremental"] = "full"
    key: str | None = Field(None, description="Column that orders changes, for incremental")


class Target(BaseModel):
    name: str
    description: str = ""
    sources: list[str] = Field(min_length=1, description="Source tables as schema.table")
    logic: str = Field(description="Joins, filters, aggregation and cleaning, in plain words")
    columns: list[TargetColumn] = Field(min_length=1)
    primary_key: list[str] = []
    load: Load = Load()
    quality_rules: list[QualityRule] = []


class EtlPlan(BaseModel):
    summary: str
    language: Language = "pyspark"
    targets: list[Target] = Field(min_length=1)
    assumptions: list[str] = Field(
        default=[],
        description="Where the request is unclear or does not match the data, and what the plan "
        "assumed instead; the user checks these before confirming",
    )


# Quality rules are checked row by row (expectations); an aggregate there cannot run.
AGGREGATE = re.compile(
    r"\b(count|count_if|sum|avg|mean|min|max|collect_list|collect_set|approx_count_distinct"
    r"|stddev|variance|percentile|percentile_approx|median)\s*\(",
    re.I,
)


class PlanError(ValueError):
    pass


def validate_plan(raw: Any, source_tables: Collection[str]) -> EtlPlan:
    """Parse the model's plan and check it against the profiled ``source_tables`` (schema.table)."""
    try:
        plan = EtlPlan.model_validate(raw)
    except ValidationError as exc:
        raise PlanError(f"方案格式不符合要求：{exc.errors(include_url=False)}") from exc
    problems = []
    names = [t.name for t in plan.targets]
    for target in plan.targets:
        if not IDENTIFIER.match(target.name):
            problems.append(f"目标表名 {target.name!r} 只能用小写字母、数字和下划线，且以字母开头")
        if target.name in RESERVED or keyword.iskeyword(target.name):
            problems.append(f"目标表名 {target.name!r} 是保留字，请换一个")
        if names.count(target.name) > 1:
            problems.append(f"目标表名 {target.name!r} 重复")
        for source in target.sources:
            if source not in source_tables and source not in names:
                problems.append(f"{target.name} 引用了不存在的源表 {source!r}")
        column_names = [c.name for c in target.columns]
        for column in column_names:
            if not IDENTIFIER.match(column):
                problems.append(f"{target.name}.{column} 列名只能用小写字母、数字和下划线")
        for key in target.primary_key:
            if key not in column_names:
                problems.append(f"{target.name} 的主键 {key!r} 不在目标列里")
        if target.load.mode == "incremental" and not target.load.key:
            problems.append(f"{target.name} 是增量加载，需要给出增量键 load.key")
        rule_names = [r.name for r in target.quality_rules]
        for rule in set(rule_names):
            if not IDENTIFIER.match(rule):
                problems.append(f"{target.name} 的质量规则名 {rule!r} 只能用小写字母、数字和下划线")
            if rule_names.count(rule) > 1:
                problems.append(f"{target.name} 的质量规则名 {rule!r} 重复")
        for quality in target.quality_rules:
            if AGGREGATE.search(quality.condition):
                problems.append(
                    f"{target.name} 的质量规则 {quality.name} 用了聚合函数"
                    f"（{quality.condition}）；质量规则逐行判断，不能用聚合，"
                    "唯一性请用 primary_key 表达"
                )
    if problems:
        raise PlanError("；".join(dict.fromkeys(problems)))
    return plan


SYSTEM_PROMPT = """你是资深数据工程师，负责把业务需求变成
Databricks Lakeflow 声明式管道的 ETL 方案。
只根据给出的源表结构、统计和样本设计方案，不要编造不存在的表或列。
清洗规则要针对样本和统计里看到的问题（空值、重复、大小写和空格不一致、负数等）。
需求里提到的取值（状态、类型、类目等）要对照“取值分布”映射成数据里的实际取值，
logic 和表达式里写实际取值；同一取值有大小写或空格不同的写法时，先 lower(trim(...)) 再比较。
数据里找不到需求提到的取值、需求有歧义、或者你加了需求没提的业务过滤时，
在 assumptions 里逐条写明你的理解和做法，交给用户确认，不要编造数据里没有的取值。
质量规则对目标表的每一行单独判断，条件里只能用这一行的列，不能用 count、sum 等聚合函数；
唯一性用 primary_key 表达，不要写成质量规则。
目标表放在 Unity Catalog 的同一个 schema 里，表名和列名用小写蛇形命名。

只输出一个 JSON 对象，不要输出其他文字，结构如下：
{
  "summary": "一两句话说明方案",
  "language": "pyspark 或 sql",
  "targets": [
    {
      "name": "目标表名",
      "description": "这张表是什么",
      "sources": ["schema.table", "也可以引用前面的目标表名"],
      "logic": "关联、过滤、聚合、清洗的做法",
      "columns": [{"name": "列名", "source": "Spark SQL 表达式", "description": ""}],
      "primary_key": ["列名"],
      "load": {"mode": "full 或 incremental", "key": "增量键列名或 null"},
      "quality_rules": [{"name": "规则名", "condition": "Spark SQL 布尔表达式",
                         "on_violation": "warn、drop 或 fail"}]
    }
  ],
  "assumptions": ["需求和数据对不上、或需要用户确认的地方，一条一句；没有就给空数组"]
}"""
