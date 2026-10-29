"""The ETL plan: what the model proposes and the user confirms before any code exists.

The model answers in JSON matching ``EtlPlan``; ``validate_plan`` then checks it
against the profiled source tables, so a plan that names a table or column that
does not exist goes back to the model instead of on to the user.
"""

import keyword
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from aep.knowledge.citations import IndexedHit

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
    references: list[IndexedHit] = []


CITATION = re.compile(r"\[([0-9]+)\]")


def _references(plan: EtlPlan, hits: list[IndexedHit]) -> None:
    """Keep only citations to retrieved hits; references are assigned by the backend."""
    allowed = {str(hit.index): hit for hit in hits}
    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        number = match[1]
        if number in allowed:
            used.add(number)
            return match[0]
        return ""

    def clean(text: str) -> str:
        return CITATION.sub(replace, text)

    plan.summary = clean(plan.summary)
    plan.assumptions = [clean(text) for text in plan.assumptions]
    for target in plan.targets:
        target.description = clean(target.description)
        for column in target.columns:
            column.description = clean(column.description)
    plan.references = sorted((allowed[n] for n in used), key=lambda hit: hit.index)


# Quality rules are checked row by row (expectations); an aggregate there cannot run.
AGGREGATE = re.compile(
    r"\b(count|count_if|sum|avg|mean|min|max|collect_list|collect_set|approx_count_distinct"
    r"|stddev|variance|percentile|percentile_approx|median)\s*\(",
    re.I,
)


class PlanError(ValueError):
    """The plan does not hold; ``problems`` lists what is wrong, one by one."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or [message]


@dataclass(frozen=True)
class KnownValues:
    """Every value the profile saw in a column; ``complete`` when the whole table was read."""

    values: frozenset[str]
    complete: bool


# A column compared with a string literal: ``lower(trim(o.status)) = 'paid'``,
# ``status IN ('a', 'b')``. Only the column name is kept (the last identifier before
# any closing parentheses); range comparisons are left alone.
COMPARED = re.compile(r"\b(\w+)\s*\)*\s*(?:==?|!=|<>)\s*'((?:[^']|'')*)'")
COMPARED_IN = re.compile(r"\b(\w+)\s*\)*\s+(?:not\s+)?in\s*\(([^)]*)\)", re.I)
LITERAL = re.compile(r"'((?:[^']|'')*)'")


def _normal(value: str) -> str:
    return value.replace("''", "'").strip().lower()


def compared_literals(expression: str) -> list[tuple[str, str]]:
    """(column, literal) pairs where ``expression`` compares a column with string literals."""
    pairs = [(c, v) for c, v in COMPARED.findall(expression)]
    for column, listed in COMPARED_IN.findall(expression):
        pairs += [(column, v) for v in LITERAL.findall(listed)]
    return [(column.lower(), value) for column, value in pairs]


# A copy of a column, at most with its case and spaces normalized: ``lower(trim(o.status))``.
COPY = re.compile(r"(?:(?:lower|upper|trim|ltrim|rtrim)\s*\(\s*)*(?:\w+\.)?(\w+)\s*\)*", re.I)


def _copies(column: TargetColumn) -> bool:
    """Whether ``column`` is a source column of the same name, so it keeps its values."""
    match = COPY.fullmatch(column.source.strip())
    return bool(match) and match.group(1).lower() == column.name.lower()


def _value_problems(
    plan: EtlPlan, known: Mapping[str, KnownValues], trust_assumptions: bool
) -> list[str]:
    """Literals the plan compares a column with that the profile never saw in it.

    Such a value is usually made up, so it goes back to the model with the actual values.
    It can still be right (rare, or only in a part of the table the sample missed, or the
    user said so), so once ``trust_assumptions`` is set a value the assumptions name stays.
    """
    explained = " ".join(plan.assumptions).lower()
    targets = {t.name: t for t in plan.targets}
    own: dict[str, set[str]] = {}

    def derived_in(name: str) -> set[str]:
        """Columns of target ``name`` whose values are not the source's: computed there, or
        copied from a column an upstream target computed."""
        if name not in own:
            own[name] = set()  # a cycle (rejected above anyway) ends here
            upstream = inputs(targets[name])
            own[name] = {
                c.name for c in targets[name].columns if not _copies(c) or c.name in upstream
            }
        return own[name]

    def inputs(target: Target) -> set[str]:
        return {c for s in target.sources if s in targets for c in derived_in(s)}

    problems = []
    for target in plan.targets:
        # Column expressions read the target's inputs: source tables keep the profiled
        # values, but a column an upstream target computed has its own.
        upstream = inputs(target)
        pairs = [
            p for c in target.columns for p in compared_literals(c.source) if p[0] not in upstream
        ]
        # Quality rules check the target's own output, where only copied source columns
        # keep the profiled values.
        kept = {c.name for c in target.columns if _copies(c)} - derived_in(target.name)
        pairs += [
            p for q in target.quality_rules for p in compared_literals(q.condition) if p[0] in kept
        ]
        for column, literal in dict.fromkeys(pairs):
            seen = known.get(column)
            if seen is None or _normal(literal) in seen.values:
                continue
            if trust_assumptions and _normal(literal) in explained:
                continue
            actual = "、".join(sorted(seen.values)) or "（全是空值）"
            where = "整张表" if seen.complete else "样本"
            problems.append(
                f"{target.name} 拿 {column} 和 '{literal}' 比较，但{where}里 {column} 的取值只有 "
                f"{actual}。需求说的取值对应其中某个时请改用实际取值；数据里确实没有时，"
                "不要编造取值，做不了的指标去掉或给 null，并在 assumptions 里说明；"
                f"确定要用 '{literal}'（比如用户指定了）就在 assumptions 里写明原因"
            )
    return problems


def validate_plan(
    raw: Any,
    source_tables: Collection[str],
    known: Mapping[str, KnownValues] | None = None,
    trust_assumptions: bool = True,
    references: list[IndexedHit] | None = None,
) -> EtlPlan:
    """Parse the model's plan and check it against the profiled ``source_tables`` (schema.table)
    and the ``known`` values of their columns (by column name, see ``aep.etl.profile``).

    ``trust_assumptions`` false: a value the data lacks goes back even if the assumptions name
    it, so the model looks at the actual values once before keeping it."""
    try:
        if isinstance(raw, dict):
            raw = {**raw, "references": []}
        plan = EtlPlan.model_validate(raw)
    except ValidationError as exc:
        raise PlanError(f"方案格式不符合要求：{exc.errors(include_url=False)}") from exc
    _references(plan, references or [])
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
    problems += _value_problems(plan, known or {}, trust_assumptions)
    if problems:
        unique = list(dict.fromkeys(problems))
        raise PlanError("；".join(unique), unique)
    return plan


# The platform's part of the plan node's prompt: the JSON it parses. How to design
# the plan is the ``etl-plan`` skill (aep/skills/builtin/etl-plan).
OUTPUT_FORMAT = """只输出一个 JSON 对象，不要输出其他文字，结构如下：
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
