"""The ETL plan: what the model proposes and the user confirms before any code exists.

The model answers in JSON matching ``EtlPlan``; ``validate_plan`` then checks it
against the profiled source tables, so a plan that names a table or column that
does not exist goes back to the model instead of on to the user.

Version 2 (``validate_plan_v2``, the flow node etl_plan_check with ``format`` 2) adds a
business brief, rules, questions for a person and an answer design; the platform tidies
what it can and sends the rest back the same way. Version 3 (``validate_plan_v3``,
``format`` 3) adds the ways of writing a table, the project's table prefix and run
parameters, and the data checks the model proposes.
"""

import json
import keyword
import re
import unicodedata
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, PrivateAttr, TypeAdapter, ValidationError, field_validator

from aep.cluster_harness import PARAM
from aep.etl import answer, checks
from aep.etl.answer import AnswerDesign
from aep.knowledge.citations import IndexedHit
from aep.projects import filter_problem

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Names the generated pipeline code already uses (see aep.etl.codegen).
RESERVED = {"source", "spark", "dp", "f", "window", "source_catalog", "param", "json"}

Language = Literal["pyspark", "sql"]


class TargetColumn(BaseModel):
    name: str
    source: str = Field(description="Spark SQL expression over the source columns")
    description: str = ""


class QualityRule(BaseModel):
    name: str
    condition: str = Field(description="Spark SQL boolean expression a valid row satisfies")
    on_violation: Literal["warn", "drop", "fail"] = "warn"


# How a target table is written. Versions 1 and 2 of the plan have full and incremental,
# both stored by overwriting the table; version 3 has the four ways of
# docs/api/data-nodes.md "写表方式".
LoadMode = Literal["full", "incremental", "overwrite", "replace_where", "append", "merge"]
PLAN_MODES = ("full", "incremental")
WRITE_MODES = ("overwrite", "replace_where", "append", "merge")


class AddColumn(BaseModel):
    """A column an existing table gets: ``after`` the column it goes after, None first."""

    name: str
    after: str | None = None


class Load(BaseModel):
    mode: LoadMode = "full"
    key: str | None = Field(None, description="Column that orders changes, for incremental")
    where: str | None = Field(None, description="replace_where: the rows to replace (Spark SQL)")
    keys: list[str] = Field([], description="merge: the key columns; empty: the primary key")
    # Set by the platform, never by the model (version 3, docs/api/data-nodes.md,
    # "表结构变化"): the columns the plan adds to the project's existing table, in the plan's
    # order. Confirming the plan confirms them; the cluster program adds no others.
    add_columns: list[AddColumn] = []

    def stored(self, primary_key: Sequence[str]) -> dict[str, Any]:
        """How the cluster program writes the table: ``mode`` (one of ``WRITE_MODES``,
        full and incremental overwrite), ``where`` and ``keys``, and ``add_columns`` when
        the plan adds columns to an existing table."""
        mode = "overwrite" if self.mode in PLAN_MODES else self.mode
        found: dict[str, Any] = {
            "mode": mode,
            "where": self.where,
            "keys": list(self.keys or primary_key),
        }
        if self.add_columns:
            found["add_columns"] = [c.model_dump() for c in self.add_columns]
        return found


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
        # The recipe may hold indexes such as split(x)[0]: count, never rewrite.
        used.update(n for n in CITATION.findall(target.logic) if n in allowed)
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
    """Every value the profile saw in a column, trimmed and lower-cased (``values``) and as
    the data writes them (``shown``, for messages); ``complete`` when the whole table was
    read. A value a read filter names (one it filters out) counts as seen."""

    values: frozenset[str]
    complete: bool
    shown: frozenset[str] = field(default=frozenset(), compare=False)


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
    plan: EtlPlan,
    known: Mapping[str, KnownValues],
    trust_assumptions: bool,
    show_values: bool = True,
) -> list[str]:
    """Literals the plan compares a column with that the profile never saw in it.

    Such a value is usually made up, so it goes back to the model with the actual values
    (without them when ``show_values`` is false: the data outbound policy keeps them from
    the model, aep.policy). It can still be right (rare, or only in a part of the table
    the sample missed, or the user said so), so once ``trust_assumptions`` is set a value
    the assumptions name stays.
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
            # '' is a blank check (trim(city) = ''), not a value the model made up.
            if seen is None or not _normal(literal) or _normal(literal) in seen.values:
                continue
            if trust_assumptions and _normal(literal) in explained:
                continue
            where = "整张表" if seen.complete else "样本"
            if show_values:
                actual = "、".join(sorted(seen.shown or seen.values)) or "（全是空值）"
                found = f"但{where}里 {column} 的取值只有 {actual}"
            else:
                found = f"但{where}里 {column} 没有这个取值（按数据外发策略不列出实际取值）"
            problems.append(
                f"{target.name} 拿 {column} 和 '{literal}' 比较，{found}。"
                "需求说的取值对应其中某个时请改用实际取值（大小写照数据写）；数据里确实没有时，"
                "不要编造取值，做不了的指标去掉或给 null，并在 assumptions 里说明；"
                f"确定要用 '{literal}'（比如用户指定了）就在 assumptions 里写明原因。"
                "不要为了通过校验改口径，比如把维度转成小写"
            )
    return problems


def validate_plan(
    raw: Any,
    source_tables: Collection[str],
    known: Mapping[str, KnownValues] | None = None,
    trust_assumptions: bool = True,
    references: list[IndexedHit] | None = None,
    show_values: bool = True,
    modes: Sequence[str] = PLAN_MODES,
) -> EtlPlan:
    """Parse the model's plan and check it against the profiled ``source_tables`` (schema.table)
    and the ``known`` values of their columns (by column name, see ``aep.etl.profile``).

    ``trust_assumptions`` false: a value the data lacks goes back even if the assumptions name
    it, so the model looks at the actual values once before keeping it. ``show_values``
    false: the problems do not list the actual values (the data outbound policy keeps
    them from the model). ``modes`` are the plan version's ``load.mode`` values."""
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
        if target.load.mode not in modes:
            problems.append(f"{target.name} 的 load.mode 只能是 {'、'.join(modes)}")
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
    problems += _value_problems(plan, known or {}, trust_assumptions, show_values)
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


# 方案第 2 版: format 2 of the flow node etl_plan_check (docs/api/data-nodes.md). The
# plan says what is counted in business words (brief), what was decided (rules), what a
# person has to decide (questions) and the answer card it ends with. The code app etl
# keeps EtlPlan, validate_plan and OUTPUT_FORMAT above as they are.

# Same rule as a flow node id (aep.runtime.flow.nodes.ID); aep.etl does not import the runtime.
ITEM_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
BASES = ("knowledge", "request", "data", "assumption", "confirmed")
BASIS_NAMES = {
    "knowledge": "口径资料",
    "request": "需求",
    "data": "数据",
    "assumption": "假设",
    "confirmed": "已确认",
}
MAX_QUESTIONS = 5
MIN_OPTIONS = 2
MAX_OPTIONS = 6
MAX_QUESTION_TEXT = 50
MAX_OPTION_LABEL = 50
MAX_OPTION_VALUE = 64
MAX_WHY = 500
MAX_AFFECTS = 20
MAX_EXAMPLES = 5


class _Item(BaseModel):
    """A metric, dimension, filter, rule or question. The platform numbers one whose id
    is missing or malformed, so ``id`` is any JSON value until then."""

    id: Any = None
    # Where the model wrote it in its reply ("rules[2]"), for problems.
    _at: str = PrivateAttr("")


class Metric(_Item):
    name: str
    unit: str | None = None
    definition: str
    column: str | None = None


class Dimension(_Item):
    name: str
    definition: str
    column: str | None = None
    examples: list[Any] | None = None


class Filter(_Item):
    text: str


class TimeRange(BaseModel):
    text: str
    column: str | None = None


class Brief(BaseModel):
    goal: str
    grain: str | None = None
    metrics: list[Metric] = []
    dimensions: list[Dimension] = []
    filters: list[Filter] = []
    time_range: TimeRange | None = None
    # Written by the platform from the result table: not checked for code.
    _generated: bool = PrivateAttr(False)

    @field_validator("metrics", "dimensions", "filters", mode="before")
    @classmethod
    def _null_is_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class Rule(_Item):
    text: str
    basis: Any = "assumption"
    # Where its text came from: "rules[2].text", "assumptions[0]", "questions[6].text".
    _text_at: str = PrivateAttr("")
    # A rule made from a question: its words by where the model wrote them (the question's
    # text and the picked option's label), checked for code each on its own.
    _parts: list[tuple[str, str]] = PrivateAttr(default_factory=list)
    # Added from the answers people gave: the platform's words, not the model's.
    _platform: bool = PrivateAttr(False)


class Option(BaseModel):
    value: Any = None
    label: str | None = None


class Question(_Item):
    text: str
    why: str | None = None
    options: list[Option] | None = None
    suggested: Any = None
    affects: list[Any] | None = None


class LabeledColumn(TargetColumn):
    label: str | None = None
    # The model wrote the label (then it is checked for code; a filled-in one is not).
    _written: bool = PrivateAttr(False)


class LabeledTarget(Target):
    columns: list[LabeledColumn] = Field(min_length=1)


class EtlPlanV2(EtlPlan):
    """A version 2 plan: ``EtlPlan`` and more, so ``EtlPlan`` reads it too."""

    targets: list[LabeledTarget] = Field(min_length=1)  # type: ignore[assignment]
    brief: Brief | None = None
    rules: list[Rule] = []
    questions: list[Question] = []
    answer: AnswerDesign | None = None


class Confirmed(TypedDict):
    """An answer a person gave to a question, remembered by the plan check."""

    label: str
    value_label: str
    actor: str | None


def bare(text: str) -> str:
    """``text`` as compared for sameness: without spaces and punctuation. A citation keeps
    its number ("…[1]" and "…[2]" differ)."""
    return "".join(
        ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


# Structure: what pydantic says, in words for the model.

WHAT = {
    "string_type": "应该是文字",
    "list_type": "应该是列表",
    "model_type": "应该是对象",
    "model_attributes_type": "应该是对象",
    "dict_type": "应该是对象",
    "int_type": "应该是整数",
    "int_parsing": "应该是整数",
    "int_from_float": "应该是整数",
    "bool_type": "应该是 true 或 false",
    "bool_parsing": "应该是 true 或 false",
}


def _path(loc: Sequence[str | int]) -> str:
    """``brief.metrics[1].name`` for a pydantic location."""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def _structure(
    exc: ValidationError, where: str = "", modes: Sequence[str] = PLAN_MODES
) -> list[str]:
    """What pydantic found wrong, in words; ``modes`` are the plan version's ``load.mode``
    values."""
    problems = []
    for error in exc.errors(include_url=False):
        kind, ctx = error["type"], error.get("ctx") or {}
        if kind == "missing" or error.get("input", "") is None:
            what = "不能为空"
        elif kind == "literal_error":
            choices = re.findall(r"'([^']*)'", str(ctx.get("expected")))
            if tuple(error["loc"][-2:]) == ("load", "mode"):
                choices = list(modes)
            what = "只能是 " + "、".join(choices)
        elif kind == "too_short":
            what = f"至少要有 {ctx.get('min_length', 1)} 项"
        else:
            what = WHAT.get(kind, error["msg"])
        path = _path([*([where] if where else []), *error["loc"]])
        problems.append(f"{path} {what}" if path else what)
    return problems


SECTIONS: dict[str, TypeAdapter[Any]] = {
    "brief": TypeAdapter(Brief | None),
    "rules": TypeAdapter(list[Rule]),
    "questions": TypeAdapter(list[Question]),
    "answer": TypeAdapter(AnswerDesign | None),
}


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_v2(
    raw: dict[str, Any],
    cls: type[EtlPlanV2] = EtlPlanV2,
    modes: Sequence[str] = PLAN_MODES,
) -> tuple[EtlPlanV2 | None, list[str]]:
    """The plan (a ``cls``), and what is wrong with its structure. Each of ``brief``,
    ``rules``, ``questions`` and ``answer`` is read on its own: a broken one is left out
    (and not generated in its place) so the rest can still be checked. The old
    ``assumptions`` become rules with ``basis`` assumption, after the model's own rules."""
    data = dict(raw)
    found = {key: data.pop(key, None) for key in SECTIONS}
    old = data.pop("assumptions", None)
    brief = found["brief"]
    goal = brief.get("goal") if isinstance(brief, dict) else None
    if _blank(data.get("summary")) and isinstance(goal, str) and goal.strip():
        data["summary"] = goal
    problems: list[str] = []
    plan: EtlPlanV2 | None = None
    try:
        plan = cls.model_validate({**data, "assumptions": [], "references": []})
    except ValidationError as exc:
        problems += _structure(exc, modes=modes)
    assumptions: list[tuple[int, str]] = []
    if old is not None and not isinstance(old, list):
        problems.append("assumptions 应该是列表")
    for i, text in enumerate(old if isinstance(old, list) else []):
        if isinstance(text, str):
            assumptions.append((i, text))
        else:
            problems.append(f"assumptions[{i}] 应该是文字")
    parsed: dict[str, Any] = {}
    for key, adapter in SECTIONS.items():
        value = found[key]
        if key in ("rules", "questions") and value is None:
            value = []
        try:
            parsed[key] = adapter.validate_python(value)
        except ValidationError as exc:
            problems += _structure(exc, key)
            parsed[key] = [] if key in ("rules", "questions") else None
    if parsed["answer"] is not None and not parsed["answer"].target.strip():
        problems.append("answer.target 不能为空")
    if plan is None:
        return None, problems
    plan.brief, plan.answer = parsed["brief"], parsed["answer"]
    plan.questions = parsed["questions"]
    plan.rules = parsed["rules"] + [Rule(text=text) for _, text in assumptions]
    if plan.brief is not None:
        for name in ("metrics", "dimensions", "filters"):
            for i, item in enumerate(getattr(plan.brief, name)):
                item._at = f"brief.{name}[{i}]"
    own = len(parsed["rules"])
    for i, rule in enumerate(plan.rules):
        if i < own:
            rule._at, rule._text_at = f"rules[{i}]", f"rules[{i}].text"
        else:
            rule._at = rule._text_at = f"assumptions[{assumptions[i - own][0]}]"
    for i, question in enumerate(plan.questions):
        question._at = f"questions[{i}]"
    for target in plan.targets:
        for column in target.columns:
            column._written = not _blank(column.label)
    return plan, problems


def _blank_texts(plan: EtlPlanV2) -> list[str]:
    """Texts that must say something, read once their invalid [n] are gone: the summary
    and the model's brief (filters, rules and questions are checked with the rest)."""
    problems = [] if plan.summary.strip() else ["summary 不能为空"]
    brief = plan.brief
    if brief is None or brief._generated:
        return problems
    if not brief.goal.strip():
        problems.append("brief.goal 不能为空")
    for item in [*brief.metrics, *brief.dimensions]:
        for key in ("name", "definition"):
            if not getattr(item, key).strip():
                problems.append(f"{item._at}.{key} 不能为空")
    if brief.time_range is not None and not brief.time_range.text.strip():
        problems.append("brief.time_range.text 不能为空")
    return problems


# Shared helpers of the steps below.


def _result(plan: EtlPlanV2) -> LabeledTarget:
    """The table the answer reads: ``answer.target``, else the last target."""
    if plan.answer is not None:
        for target in plan.targets:
            if target.name == plan.answer.target:
                return target
    return plan.targets[-1]


CATEGORIES = (("m", "指标"), ("d", "维度"), ("f", "筛选"), ("r", "规则"), ("q", "问题"))


def _categories(plan: EtlPlanV2) -> list[tuple[str, str, Sequence[_Item]]]:
    brief = plan.brief
    lists: list[Sequence[_Item]] = [
        brief.metrics if brief else [],
        brief.dimensions if brief else [],
        brief.filters if brief else [],
        plan.rules,
        plan.questions,
    ]
    return [(prefix, what, items) for (prefix, what), items in zip(CATEGORIES, lists, strict=True)]


def _ids(plan: EtlPlanV2) -> set[str]:
    return {i.id for _, _, items in _categories(plan) for i in items if isinstance(i.id, str)}


def _free(prefix: str, used: set[str]) -> str:
    """The smallest ``{prefix}{k}`` no id uses yet; it is used from now on."""
    k = 1
    while f"{prefix}{k}" in used:
        k += 1
    used.add(f"{prefix}{k}")
    return f"{prefix}{k}"


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _new_rule(plan: EtlPlanV2, text: str, basis: str, question: Question) -> Rule:
    rule = Rule(id=_free("r", _ids(plan)), text=text, basis=basis)
    rule._at, rule._text_at = question._at, f"{question._at}.text"
    plan.rules.append(rule)
    return rule


def _question_rule(plan: EtlPlanV2, q: Question, picked: int | None) -> Rule:
    """A question that is not asked, as a rule with ``basis`` assumption: "{题目}——按
    「{选项名称}」处理" with option ``picked``'s label, or the question alone when there is
    no such option or it has no label (an option's value is no business text)."""
    option = (q.options or [])[picked] if picked is not None else None
    label = option.label if option is not None and not _blank(option.label) else None
    rule = _new_rule(plan, f"{q.text}——按「{label}」处理" if label else q.text, "assumption", q)
    rule._parts = [(f"{q._at}.text", q.text)]
    if label:
        rule._parts.append((f"{q._at}.options[{picked}].label", label))
    return rule


# 平台自动整理: each step goes over the whole plan before the next one starts, and says
# what it did in ``cleaned``.

# A lowercase English word before ":", "：", "—" or "-" ("advance:"); not "e-commerce".
PREFIX = re.compile(r"^\s*([a-z]+)\s*(:|：|—|-(?![A-Za-z0-9]))\s*")
NUMBERING = re.compile(r"^\s*(?:\d{1,3}[.．、](?!\d)|[(（]\d{1,3}[)）]|[①-⑳]|-\s)\s*")
LEAD = re.compile(r"^\s*(?:假设|规则|问题|说明|注意|备注)\s*[：:]\s*")


def _text_lists(plan: EtlPlanV2) -> Iterator[tuple[Callable[[], str], Sequence[Any], str]]:
    """The texts the first three steps tidy, each list with its name: the model's filters,
    the rules, the questions and each question's option labels."""
    if plan.brief is not None and not plan.brief._generated:
        yield lambda: "筛选", plan.brief.filters, "text"
    yield lambda: "规则", plan.rules, "text"
    yield lambda: "问题", plan.questions, "text"
    for question in plan.questions:
        if question.options:
            yield (lambda q=question: f"问题“{q.text}”的选项"), question.options, "label"


def _drop_prefixes(plan: EtlPlanV2, cleaned: list[str]) -> None:
    for name, items, attr in _text_lists(plan):
        words: dict[str, list[tuple[Any, re.Match[str]]]] = {}
        for item in items:
            text = getattr(item, attr)
            match = PREFIX.match(text) if isinstance(text, str) else None
            if match:
                words.setdefault(match[1], []).append((item, match))
        for word, found in words.items():
            if len(found) < 2:
                continue
            for item, match in found:
                setattr(item, attr, getattr(item, attr)[match.end() :])
            separator = found[0][1][2]
            cleaned.append(f"去掉了 {len(found)} 条{name()}开头的英文前缀“{word}{separator}”")


def _unnumbered(text: str) -> str:
    while True:
        tidy = LEAD.sub("", NUMBERING.sub("", text, count=1), count=1)
        if tidy == text:
            return text
        text = tidy


def _drop_numbering(plan: EtlPlanV2, cleaned: list[str]) -> None:
    for name, items, attr in _text_lists(plan):
        count = 0
        for item in items:
            text = getattr(item, attr)
            if isinstance(text, str) and _unnumbered(text) != text:
                setattr(item, attr, _unnumbered(text))
                count += 1
        if count:
            cleaned.append(f"去掉了 {count} 条{name()}开头的编号或“假设：”这类前缀")


def _drop_repeats(plan: EtlPlanV2, cleaned: list[str]) -> None:
    def unique(items: list[Any], what: str) -> list[Any]:
        kept, seen = [], set()
        for item in items:
            key = bare(item.text)
            if key and key in seen:
                continue
            seen.add(key)
            kept.append(item)
        if len(kept) < len(items):
            cleaned.append(f"去掉了 {len(items) - len(kept)} 条重复的{what}")
        return kept

    if plan.brief is not None:
        plan.brief.filters = unique(plan.brief.filters, "筛选")
    plan.rules = unique(plan.rules, "规则")
    plan.questions = unique(plan.questions, "问题")


def _known_bases(plan: EtlPlanV2, cleaned: list[str]) -> None:
    unknown = 0
    for rule in plan.rules:
        if rule.basis is None:
            rule.basis = "assumption"
        elif not isinstance(rule.basis, str) or rule.basis not in BASES:
            rule.basis = "assumption"
            unknown += 1
    if unknown:
        cleaned.append(f"{unknown} 条规则的依据 basis 不认识，按 assumption（假设）处理")


def _label_or_name(column: LabeledColumn) -> str:
    return column.label if column.label is not None and column.label.strip() else column.name


def _generate_brief(plan: EtlPlanV2, cleaned: list[str]) -> None:
    """A brief from the result table: its primary key as dimensions, the other columns as
    metrics, numbered in column order."""
    table = _result(plan)
    used = _ids(plan)
    keys = set(table.primary_key)
    dimensions, metrics = [], []
    for c in table.columns:
        name, definition = _label_or_name(c), c.description
        if c.name in keys:
            dimensions.append(
                Dimension(id=_free("d", used), name=name, definition=definition, column=c.name)
            )
        else:
            metrics.append(
                Metric(id=_free("m", used), name=name, definition=definition, column=c.name)
            )
    by_name = {c.name: c for c in table.columns}
    grain = [_label_or_name(by_name[k]) for k in table.primary_key if k in by_name]
    plan.brief = Brief(
        goal=plan.summary,
        grain=f"每个{' × '.join(grain)}一行" if grain else None,
        metrics=metrics,
        dimensions=dimensions,
    )
    plan.brief._generated = True
    cleaned.append(
        f"模型没写业务说明，平台按结果表 {table.name} 的列生成了一份（主键列当维度，其余列当指标）"
    )


def _fill_labels(plan: EtlPlanV2, cleaned: list[str]) -> None:
    """A result column's label is its metric's "name（unit）" or its dimension's name;
    any other column without one gets its name."""
    named: dict[str, str] = {}
    if plan.brief is not None:
        for d in reversed(plan.brief.dimensions):
            if d.column is not None:
                named[d.column] = d.name
        for m in reversed(plan.brief.metrics):
            if m.column is not None:
                named[m.column] = f"{m.name}（{m.unit}）" if not _blank(m.unit) else m.name
    result = _result(plan)
    for target in plan.targets:
        filled = 0
        for c in target.columns:
            if _blank(c.label):
                c.label = (named.get(c.name) if target is result else None) or c.name
                filled += 1
        if filled:
            cleaned.append(f"给 {target.name} 的 {filled} 列补了显示名称 label")


def _renumber(plan: EtlPlanV2, cleaned: list[str]) -> None:
    """Ids not written or not like a node id become the smallest free one of their kind;
    ``affects`` follows, then loses ids that name nothing in the plan."""
    used = _ids(plan)
    moved: dict[Any, str] = {}
    for prefix, what, items in _categories(plan):
        unnumbered = []
        for item in items:
            if isinstance(item.id, str) and ITEM_ID.match(item.id):
                continue
            new = _free(prefix, used)
            if _blank(item.id):
                unnumbered.append(new)
            else:
                cleaned.append(f"{what}的编号“{_shown(item.id)}”写法不对，改成了 {new}")
                if isinstance(item.id, str | int | float):
                    moved.setdefault(item.id, new)
            item.id = new
        if unnumbered:
            listed = "、".join(unnumbered)
            cleaned.append(f"给 {len(unnumbered)} 个没写编号的{what}编了号：{listed}")
    named = _ids(plan)
    for q in plan.questions:
        if q.affects is None:
            continue
        mapped = [moved.get(a, a) if isinstance(a, str | int | float) else a for a in q.affects]
        q.affects = [a for a in mapped if isinstance(a, str) and a in named]
        gone = [_shown(a) for a in mapped if not (isinstance(a, str) and a in named)]
        if gone:
            cleaned.append(f"去掉了问题 {q.id} 的 affects 里不存在的 {'、'.join(gone)}")


def _tidy_questions(plan: EtlPlanV2, cleaned: list[str]) -> None:
    def text(value: Any) -> Any:
        return json.dumps(value) if isinstance(value, bool | int | float) else value

    for q in plan.questions:
        values = [o.value for o in q.options or []] + [q.suggested]
        for option in q.options or []:
            option.value = text(option.value)
        q.suggested = text(q.suggested)
        if any(isinstance(v, bool | int | float) for v in values):
            cleaned.append(f"问题 {q.id} 的选项值或建议答案从数字或 true、false 转成了文字")
        if q.why is not None and len(q.why) > MAX_WHY:
            q.why = q.why[:MAX_WHY]
            cleaned.append(f"问题 {q.id} 的“为什么问”超过 {MAX_WHY} 个字符，截断了")
        if q.affects:
            unique = list(dict.fromkeys(q.affects))
            if len(unique) < len(q.affects):
                cleaned.append(f"问题 {q.id} 的 affects 去掉了重复的")
            if len(unique) > MAX_AFFECTS:
                unique = unique[:MAX_AFFECTS]
                cleaned.append(f"问题 {q.id} 的 affects 只留了前 {MAX_AFFECTS} 个")
            q.affects = unique


def _few_options(plan: EtlPlanV2, cleaned: list[str]) -> None:
    """A question with fewer than 2 options is no question: it becomes a rule."""
    kept = []
    for q in plan.questions:
        options = q.options or []
        if len(options) >= MIN_OPTIONS:
            kept.append(q)
            continue
        rule = _question_rule(plan, q, 0 if options else None)
        cleaned.append(f"问题“{q.text}”的选项少于 {MIN_OPTIONS} 个，改成了规则 {rule.id}")
    plan.questions = kept


# Seen values: per profiled table (schema.table), per lower-case column name; None for a
# column whose values the profile did not all list (aep.etl.profile.seen_values).
Seen = Mapping[str, Mapping[str, Collection[str] | None]]

# A column an expression reads: ``city``, ``c.city`` or ``sales.customers.city`` (the
# qualifier is a table or an alias). Not a function name (before "("), the type of a typed
# literal (before a quote: DATE '2026-01-01'), a type after AS or an SQL keyword.
REFERENCE = re.compile(r"(?<![\w.])((?:[A-Za-z_]\w*\.)*)([A-Za-z_]\w*)(?![\w.]|\s*[('])")
AFTER_AS = re.compile(r"\bas\s*$", re.I)
KEYWORDS = frozenset(
    {
        "and", "or", "not", "is", "null", "in", "like", "rlike", "ilike", "between", "case",
        "when", "then", "else", "end", "distinct", "true", "false", "as", "over", "partition",
        "by", "order", "asc", "desc", "nulls", "rows", "range", "unbounded", "preceding",
        "following", "interval", "div",
    }
)  # fmt: skip


def _read_columns(expression: str) -> list[tuple[str, str]]:
    """(qualifier, lower-case column name) of the columns ``expression`` reads."""
    text = LITERAL.sub("''", expression.replace("`", ""))
    return [
        (m[1].rstrip(".").lower(), m[2].lower())
        for m in REFERENCE.finditer(text)
        if m[2].lower() not in KEYWORDS and not AFTER_AS.search(text, 0, m.start())
    ]


def _short(table: str) -> str:
    return table.lower().rsplit(".", 1)[-1]


def _qualified(qualifier: str, tables: Sequence[str]) -> list[str]:
    """The tables a column's qualifier points to: the table itself (``customers`` or
    ``sales.customers``), else the one whose name it shortens (``c`` or ``cust`` for
    customers, ``oi`` for order_items); every one when there is none or it is unclear."""
    if not qualifier:
        return list(tables)
    named = [t for t in tables if qualifier in (t.lower(), _short(t))]
    if named:
        return named
    shortened = [
        t
        for t in tables
        if _short(t).startswith(qualifier)
        or qualifier == "".join(word[0] for word in _short(t).split("_") if word)
    ]
    return shortened if len(shortened) == 1 else list(tables)


def _data_values(
    plan: EtlPlanV2, target: Target, column: TargetColumn, seen: Seen
) -> set[str] | None:
    """The values a column can hold: what the profile listed for the source columns its
    ``source`` reads, in the tables the target reads (through upstream targets too), and
    the string literals there. None when that cannot be told: a source column whose
    values were not all listed (a time, an amount) or that the profile does not know."""
    targets = {t.name: t for t in plan.targets}
    values: set[str] = set()
    visited: set[tuple[str, str]] = set()
    unknown = False

    def walk(table: Target, expression: str) -> None:
        nonlocal unknown
        values.update(_normal(v) for v in LITERAL.findall(expression))
        for qualifier, name in _read_columns(expression):
            found = False
            for source in _qualified(qualifier, table.sources):
                if source in targets:
                    for c in targets[source].columns:
                        if c.name.lower() != name:
                            continue
                        found = True
                        if (source, c.name) not in visited:
                            visited.add((source, c.name))
                            walk(targets[source], c.source)
                    continue
                columns = seen.get(source) or {}
                if name not in columns:
                    continue
                found = True
                listed = columns[name]
                if listed is None:
                    unknown = True
                else:
                    values.update(listed)
            if not found:
                unknown = True

    walk(target, column.source)
    return None if unknown else values


def _tidy_examples(plan: EtlPlanV2, seen: Seen, cleaned: list[str]) -> None:
    """A dimension's examples keep the values the data has; when the profile cannot tell
    (no result column, or values not all listed) they stay as the model wrote them."""
    if plan.brief is None:
        return
    result = _result(plan)
    columns = {c.name: c for c in result.columns}
    for d in plan.brief.dimensions:
        if d.examples is None:
            continue
        column = columns.get(d.column) if d.column is not None else None
        allowed = _data_values(plan, result, column, seen) if column else None
        kept = [
            e
            for e in d.examples
            if isinstance(e, str) and (allowed is None or e.strip().lower() in allowed)
        ]
        gone = [_shown(e) for e in d.examples if not (isinstance(e, str) and e in kept)]
        if gone:
            cleaned.append(f"维度“{d.name}”的例子去掉了探查里没见过的 {'、'.join(gone)}")
        if len(kept) > MAX_EXAMPLES:
            kept = kept[:MAX_EXAMPLES]
            cleaned.append(f"维度“{d.name}”的例子只留前 {MAX_EXAMPLES} 个")
        d.examples = kept


def _generate_answer(plan: EtlPlanV2, cleaned: list[str]) -> None:
    if plan.brief is None:
        return
    table = _result(plan)
    names = [c.name for c in table.columns]
    dimensions = [d.column for d in plan.brief.dimensions if d.column and d.column in names]
    metrics = [m.column for m in plan.brief.metrics if m.column and m.column in names]
    time = plan.brief.time_range.column if plan.brief.time_range else None
    design = answer.generate(table.name, names, dimensions, metrics, time)
    if design is not None:
        plan.answer = design
        cleaned.append("模型没写答案设计，平台按业务说明生成了一份")


def _confirm(plan: EtlPlanV2, answered: Mapping[str, Confirmed]) -> None:
    """A question a person already answered (in any round: compared by its text, never by
    id) becomes a rule with ``basis`` confirmed, unless the plan has that rule already."""
    kept = []
    for q in plan.questions:
        key = bare(q.text)
        given = answered.get(key) if key else None
        if given is None:
            kept.append(q)
            continue
        text = f"{given['actor'] or ''}已确认：{given['label']}——{given['value_label']}"
        if not any(r.basis == "confirmed" and bare(r.text) == bare(text) for r in plan.rules):
            _new_rule(plan, text, "confirmed", q)._platform = True
    plan.questions = kept


def _overflow(plan: EtlPlanV2, cleaned: list[str]) -> None:
    """Questions from the 6th on become rules, as their suggested option says."""
    extra = plan.questions[MAX_QUESTIONS:]
    if not extra:
        return
    plan.questions = plan.questions[:MAX_QUESTIONS]
    made = []
    for q in extra:
        values = [o.value for o in q.options or []]
        known = isinstance(q.suggested, str) and q.suggested in values
        made.append(_question_rule(plan, q, values.index(q.suggested) if known else None).id)
    cleaned.append(
        f"问题最多 {MAX_QUESTIONS} 个，第 {MAX_QUESTIONS + 1} 题起的 {len(extra)} 题"
        f"按建议的选项改成了规则 {'、'.join(made)}"
    )


def _cite_v2(plan: EtlPlanV2, hits: list[IndexedHit]) -> None:
    """``_references`` over the version 2 texts too: the brief, rules and questions."""
    _references(plan, hits)
    allowed = {str(hit.index): hit for hit in hits}
    used = {str(hit.index) for hit in plan.references}

    def replace(match: re.Match[str]) -> str:
        if match[1] in allowed:
            used.add(match[1])
            return match[0]
        return ""

    def clean(text: str) -> str:
        return CITATION.sub(replace, text)

    def maybe(text: str | None) -> str | None:
        return None if text is None else clean(text)

    if plan.brief is not None:
        brief = plan.brief
        brief.goal, brief.grain = clean(brief.goal), maybe(brief.grain)
        for item in [*brief.metrics, *brief.dimensions]:
            item.definition = clean(item.definition)
        for f in brief.filters:
            f.text = clean(f.text)
        if brief.time_range is not None:
            brief.time_range.text = clean(brief.time_range.text)
    for rule in plan.rules:
        rule.text = clean(rule.text)
    for q in plan.questions:
        q.text, q.why = clean(q.text), maybe(q.why)
        for option in q.options or []:
            option.label = maybe(option.label)
    plan.references = sorted((allowed[n] for n in used), key=lambda hit: hit.index)


# Checks that send the plan back to the model.

# Code in business words: a backtick, F.fn(, CASE WHEN, or a common function name right
# before a half-width "(" with no ASCII letter, digit or "_" in front ("按count(" is code,
# "Discount(" and "GMV(含税)" are not).
FUNCTIONS = (
    "col", "lower", "upper", "trim", "coalesce", "cast", "nvl", "ifnull", "concat", "substr",
    "regexp_replace", "date_format", "date_trunc", "to_date", "sum", "count", "avg",
)  # fmt: skip
CODE = re.compile(
    r"`"
    r"|(?<![A-Za-z0-9_])F\.[A-Za-z_]\w*\("
    r"|(?<![A-Za-z0-9_])(?i:case\s+when)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?i:" + "|".join(FUNCTIONS) + r")\("
)


# PySpark where Spark SQL belongs (a column's source, a quality rule's condition): the
# answer's additivity and the generated code read those as SQL.
PYSPARK = re.compile(r"(?<![\w.])(?:F\.[A-Za-z_]\w*|col|lit)\(|\.(?:alias|cast|otherwise)\(")


def _pyspark_problems(plan: EtlPlanV2) -> list[str]:
    problems = []
    for i, target in enumerate(plan.targets):
        found = [
            (c.name, match[0])
            for c in target.columns
            if (match := PYSPARK.search(LITERAL.sub("''", c.source)))
        ]
        found += [
            (q.name, match[0])
            for q in target.quality_rules
            if (match := PYSPARK.search(LITERAL.sub("''", q.condition)))
        ]
        if found:
            names = "、".join(name for name, _ in found)
            problems.append(
                f"targets[{i}] 的 {names} 写成了 PySpark（{found[0][1]}）：列的 source 和"
                "质量规则的 condition 要写 Spark SQL 表达式，如 sum(quantity)、count(order_id)"
            )
    return problems


def _code_in(text: str | None) -> str | None:
    match = CODE.search(CITATION.sub("", text)) if text else None
    return match[0] if match else None


def _business_texts(plan: EtlPlanV2) -> Iterator[tuple[str, str | None]]:
    """(where, text) of every business text the model wrote."""
    brief = plan.brief
    if brief is not None and not brief._generated:
        yield "brief.goal", brief.goal
        yield "brief.grain", brief.grain
        for m in brief.metrics:
            yield from ((f"{m._at}.{k}", getattr(m, k)) for k in ("name", "definition", "unit"))
        for d in brief.dimensions:
            yield from ((f"{d._at}.{k}", getattr(d, k)) for k in ("name", "definition"))
        for f in brief.filters:
            yield f"{f._at}.text", f.text
        if brief.time_range is not None:
            yield "brief.time_range.text", brief.time_range.text
    for rule in plan.rules:
        if not rule._platform:
            yield from rule._parts or [(rule._text_at, rule.text)]
    for q in plan.questions:
        yield f"{q._at}.text", q.text
        yield f"{q._at}.why", q.why
        for j, option in enumerate(q.options or []):
            yield f"{q._at}.options[{j}].label", option.label
    for i, target in enumerate(plan.targets):
        for j, column in enumerate(target.columns):
            if column._written:
                yield f"targets[{i}].columns[{j}].label", column.label


def _question_problems(q: Question) -> list[str]:
    """The human node takes the question as it is (flow-apps.md, 确认模式的题 fields)."""
    at, problems = q._at, []
    if not q.text.strip():
        problems.append(f"{at}.text 不能为空")
    elif len(q.text) > MAX_QUESTION_TEXT:
        problems.append(f"{at}.text 最多 {MAX_QUESTION_TEXT} 个字符，解释写在 why 里")
    options = q.options or []
    if len(options) > MAX_OPTIONS:
        problems.append(f"{at}.options 最多 {MAX_OPTIONS} 个")
    values: list[str] = []
    for j, option in enumerate(options):
        value = option.value
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_OPTION_VALUE:
            problems.append(f"{at}.options[{j}].value 要是 1–{MAX_OPTION_VALUE} 个字符的文字")
        elif value in values:
            problems.append(f"{at}.options[{j}].value {value} 重复")
        else:
            values.append(value)
        if _blank(option.label):
            problems.append(f"{at}.options[{j}].label 不能为空")
        elif len(option.label or "") > MAX_OPTION_LABEL:
            problems.append(f"{at}.options[{j}].label 最多 {MAX_OPTION_LABEL} 个字符")
    if _blank(q.suggested):
        problems.append(f"{at}.suggested 要写方案现在按哪个选项做（某个选项的 value）")
    elif q.suggested not in values:
        problems.append(f"{at}.suggested {_shown(q.suggested)} 不在选项里")
    return problems


def _version_one_checks(
    plan: EtlPlanV2,
    source_tables: Collection[str],
    known: Mapping[str, KnownValues] | None,
    trust_assumptions: bool,
    show_values: bool = True,
    modes: Sequence[str] = PLAN_MODES,
) -> list[str]:
    problems: list[str] = []
    # Version 1's checks; the texts a value may be explained in are the rules and the
    # questions (version 1 reads them from assumptions).
    explained = [r.text for r in plan.rules]
    for q in plan.questions:
        explained += [q.text, q.why or "", *(o.label or "" for o in q.options or [])]
    projection = plan.model_dump(include={"summary", "language", "targets"})
    projection["assumptions"] = explained
    try:
        validate_plan(projection, source_tables, known, trust_assumptions, None, show_values, modes)
    except PlanError as exc:
        problems += exc.problems
    return problems


def _version_two_checks(plan: EtlPlanV2, model_answer: bool = True) -> list[str]:
    """``model_answer``: the model wrote the answer design (the platform's own design from
    the brief is not checked for its groups: the card says which groups it cannot add up)."""
    problems = _pyspark_problems(plan)
    for where, text in _business_texts(plan):
        found = _code_in(text)
        if found:
            problems.append(
                f"{where} 里有代码（{found}）：业务说明给人看，用文字写，表达式写在目标表的列里"
            )
    result = _result(plan)
    names = {c.name for c in result.columns}
    brief = plan.brief
    if brief is not None and not brief._generated:
        columns = [(f"{i._at}.column", i.column) for i in [*brief.metrics, *brief.dimensions]]
        if brief.time_range is not None:
            columns.append(("brief.time_range.column", brief.time_range.column))
        for where, column in columns:
            if column is not None and column not in names:
                problems.append(f"{where} {column} 不是结果表 {result.name} 的列")
    places: dict[str, list[str]] = {}
    for _, _, items in _categories(plan):
        for item in items:
            places.setdefault(item.id, []).append(item._at)
    for item_id, at in places.items():
        if len(at) > 1:
            problems.append(
                f"id {item_id} 重复（{'、'.join(at)}），指标、维度、筛选、规则和问题的 id 不能重复"
            )
    for f in brief.filters if brief is not None else []:
        if not f.text.strip():
            problems.append(f"{f._at}.text 不能为空")
    for rule in plan.rules:
        if not rule.text.strip():
            problems.append(f"{rule._text_at} 不能为空")
    for q in plan.questions:
        problems += _question_problems(q)
    if plan.answer is not None:
        targets = {t.name: [c.name for c in t.columns] for t in plan.targets}
        keys = {t.name: list(t.primary_key) for t in plan.targets} if model_answer else None
        sources = {t.name: {c.name: c.source for c in t.columns} for t in plan.targets}
        problems += answer.design_problems(plan.answer, targets, keys, sources)
    return problems


def validate_plan_v2(
    raw: Any,
    source_tables: Collection[str],
    known: Mapping[str, KnownValues] | None = None,
    seen: Seen | None = None,
    trust_assumptions: bool = True,
    references: list[IndexedHit] | None = None,
    answered: Mapping[str, Confirmed] | None = None,
    show_values: bool = True,
) -> tuple[EtlPlanV2, list[str]]:
    """Parse and tidy a version 2 plan, then check it like ``validate_plan`` and more.

    ``seen`` holds every value the profile showed per table and column name (``aep.etl.
    profile.seen_values``), for a dimension's examples; ``answered`` the answers people gave, by
    ``bare`` question text. Returns the plan and what the platform tidied (``cleaned``);
    raises ``PlanError`` with what the model has to rewrite."""
    return _validated(
        raw, source_tables, known, seen, trust_assumptions, references, answered, show_values
    )


def _validated(
    raw: Any,
    source_tables: Collection[str],
    known: Mapping[str, KnownValues] | None,
    seen: Seen | None,
    trust_assumptions: bool,
    references: list[IndexedHit] | None,
    answered: Mapping[str, Confirmed] | None,
    show_values: bool,
    version: Literal[2, 3] = 2,
    project: Mapping[str, Any] | None = None,
    existing: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[EtlPlanV2, list[str]]:
    """Version 2 or 3 of the plan: parsed, tidied and checked (see the two callers)."""
    if not isinstance(raw, dict):
        raise PlanError("方案要是一个 JSON 对象")
    model_brief = raw.get("brief") is not None
    model_answer = raw.get("answer") is not None
    modes: Sequence[str] = PLAN_MODES
    proposed: Any = None
    if version == 3:
        modes = WRITE_MODES
        raw = _overwrite_by_default(raw)
        proposed = _tidy_checks(raw.pop("checks", None))
    plan, structure = _parse_v2(raw, EtlPlanV3 if version == 3 else EtlPlanV2, modes)
    if plan is None:
        raise PlanError("；".join(structure), structure)
    cleaned: list[str] = []
    _tidy_rule_names(plan, cleaned)
    if version == 3 and project is not None:
        _pyspark_for_filters(plan, project, cleaned)
    _drop_prefixes(plan, cleaned)
    _drop_numbering(plan, cleaned)
    _drop_repeats(plan, cleaned)
    _known_bases(plan, cleaned)
    if not model_brief:
        _generate_brief(plan, cleaned)
    _fill_labels(plan, cleaned)
    _renumber(plan, cleaned)
    _tidy_questions(plan, cleaned)
    _few_options(plan, cleaned)
    _tidy_examples(plan, seen or {}, cleaned)
    if not model_answer:
        _generate_answer(plan, cleaned)
    _confirm(plan, answered or {})
    _overflow(plan, cleaned)
    _cite_v2(plan, references or [])
    problems = _version_one_checks(
        plan, source_tables, known, trust_assumptions, show_values, modes
    )
    problems += structure + _blank_texts(plan) + _version_two_checks(plan, model_answer)
    kept: list[dict[str, Any]] = []
    if version == 3:
        kept, more = _version_three_checks(plan, proposed, project, existing)
        problems += more
    if problems:
        unique = list(dict.fromkeys(problems))
        raise PlanError("；".join(unique), unique)
    plan.assumptions = []
    if plan.answer is not None:
        plan.answer = answer.with_defaults(plan.answer)
    if isinstance(plan, EtlPlanV3):
        plan.checks = kept
    return plan, cleaned


def uncited(plan: EtlPlanV2) -> list[str]:
    """Ids of the metrics, dimensions and filters whose words cite no source."""
    if plan.brief is None:
        return []
    texts = [(m.id, m.definition) for m in plan.brief.metrics]
    texts += [(d.id, d.definition) for d in plan.brief.dimensions]
    texts += [(f.id, f.text) for f in plan.brief.filters]
    return [item_id for item_id, text in texts if not CITATION.search(text)]


def answer_names(plan: EtlPlanV2) -> dict[str, str]:
    """What the brief calls the result table's columns: the first metric, else the first
    dimension, naming each column."""
    names: dict[str, str] = {}
    if plan.brief is not None:
        for d in reversed(plan.brief.dimensions):
            if d.column is not None:
                names[d.column] = d.name
        for m in reversed(plan.brief.metrics):
            if m.column is not None:
                names[m.column] = m.name
    return names


def answer_output(plan: EtlPlanV2) -> dict[str, Any] | None:
    """The answer design with its columns and sentence (docs/api/data-nodes.md)."""
    if plan.answer is None:
        return None
    table = _result(plan)
    sources = {c.name: c.source for c in table.columns}
    labels = {c.name: _label_or_name(c) for c in table.columns}
    units: dict[str, str | None] = {}
    if plan.brief is not None:
        for m in reversed(plan.brief.metrics):
            if m.column is not None:
                units[m.column] = None if _blank(m.unit) else m.unit
    return plan.answer.model_dump() | {
        "columns": answer.columns(plan.answer, sources, labels, units),
        "text": answer.text(plan.answer, labels | answer_names(plan)),
    }


# The platform's part of the plan node's prompt for version 2: the JSON it parses. How
# to write such a plan is the ``etl-plan-brief`` skill (aep/skills/builtin/etl-plan-brief).
OUTPUT_FORMAT_V2 = """只输出一个 JSON 对象，不要输出其他文字，结构如下：
{
  "summary": "一两句话说明方案",
  "language": "pyspark 或 sql",
  "brief": {
    "goal": "要统计什么，一两句话",
    "grain": "结果表一行是什么，如“每个统计月 × 城市一行”",
    "metrics": [{"id": "m1", "name": "指标名称", "unit": "单位，没有时为 null",
                 "definition": "怎么算，用文字写", "column": "结果表里的列名"}],
    "dimensions": [{"id": "d1", "name": "维度名称", "definition": "取自哪里、怎么归一",
                    "column": "结果表里的列名", "examples": ["数据里真有的取值，最多 5 个"]}],
    "filters": [{"id": "f1", "text": "筛选条件"}],
    "time_range": {"text": "时间范围和按哪个时间算", "column": "结果表里的时间列"}
  },
  "targets": [
    {
      "name": "目标表名",
      "description": "这张表是什么",
      "sources": ["schema.table", "也可以引用前面的目标表名"],
      "logic": "关联、过滤、聚合、清洗的做法",
      "columns": [{"name": "列名", "label": "给人看的列名，如“销售额（元）”",
                   "source": "Spark SQL 表达式", "description": ""}],
      "primary_key": ["列名"],
      "load": {"mode": "full 或 incremental", "key": "增量键列名或 null"},
      "quality_rules": [{"name": "规则名", "condition": "Spark SQL 布尔表达式",
                         "on_violation": "warn、drop 或 fail"}]
    }
  ],
  "rules": [{"id": "r1", "text": "已经定下的处理，一条一句",
             "basis": "knowledge、request、data、assumption 或 confirmed"}],
  "questions": [{"id": "q1", "text": "要人拍板的问题，最多 50 个字", "why": "为什么问",
                 "options": [{"value": "选项值", "label": "选项名称"}],
                 "suggested": "方案现在按哪个选项写（选项的 value）", "affects": ["m1"]}],
  "answer": {"target": "结果表名", "filter": [{"column": "时间列", "pick": "latest"}],
             "group_by": ["分组列，最多 3 个"], "measures": ["指标列，1–8 个"],
             "order_by": [{"column": "排序列", "desc": true}], "top_n": 10,
             "chart": {"type": "bar、line 或 none", "label": ["分组列"], "value": "指标列"},
             "add_up": ["可以跨行相加、但不是 sum 或 count 算出来的指标列，没有就给空数组"]}
}
不限时间时 time_range 给 null；没有要问的 questions 给空数组，最多 5 题，每题 2–6 个选项。"""


# 方案第 3 版: format 3 of the flow node etl_plan_check (docs/api/data-nodes.md, 第三批).
# Version 2 and: the four ways of writing a table, the run's project (table prefix,
# reference tables, run parameters) and the data checks the model proposes, in the shape
# of the node data_check's checks (aep.etl.checks), so they can be included there.

MAX_PLAN_CHECKS = 10
PLAN_CHECK_KINDS = ("aggregate", "reconcile", "unique", "rows")
UNBOUND = "这次运行没有绑定项目，不能用运行参数"


class EtlPlanV3(EtlPlanV2):
    """A version 3 plan: ``EtlPlanV2`` and the data checks it proposes (``checks``)."""

    checks: list[dict[str, Any]] = []


def validate_plan_v3(
    raw: Any,
    source_tables: Collection[str],
    known: Mapping[str, KnownValues] | None = None,
    seen: Seen | None = None,
    trust_assumptions: bool = True,
    references: list[IndexedHit] | None = None,
    answered: Mapping[str, Confirmed] | None = None,
    show_values: bool = True,
    project: Mapping[str, Any] | None = None,
    existing: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[EtlPlanV3, list[str]]:
    """Parse, tidy and check a version 3 plan: ``validate_plan_v2`` and the version's own
    rules. ``project`` is the run's project as the run keeps it (``table_prefix``,
    ``references``, ``params``), None when the run is bound to none. ``existing`` are
    the project's tables already in the run's environment (the profile node's
    ``existing``: ``name`` and ``columns``); a target with one's name writes to it, so the
    plan keeps its columns and the columns it adds go into ``load.add_columns``. None:
    not known, nothing is added."""
    plan, cleaned = _validated(
        raw,
        source_tables,
        known,
        seen,
        trust_assumptions,
        references,
        answered,
        show_values,
        3,
        project,
        existing,
    )
    assert isinstance(plan, EtlPlanV3)
    return plan, cleaned


def result_table(plan: EtlPlanV2) -> LabeledTarget:
    """The plan's result table (the one its answer reads, else the last target)."""
    return _result(plan)


def _overwrite_by_default(raw: dict[str, Any]) -> dict[str, Any]:
    """``raw`` with ``load.mode`` overwrite where a target leaves it out, and the load's
    unused parts left out when the model wrote them empty (``where`` blank, ``keys`` or
    ``key`` null): the format says to write those as null or [], and either will do. A
    model's ``add_columns`` is dropped: the platform works them out."""
    data = dict(raw)
    targets = data.get("targets")
    if not isinstance(targets, list):
        return data
    written = []
    for target in targets:
        if isinstance(target, dict):
            load = target.get("load")
            if load is None:
                target = {**target, "load": {"mode": "overwrite"}}
            elif isinstance(load, dict):
                # add_columns is the platform's to set, from the existing table.
                load = {
                    k: v
                    for k, v in load.items()
                    if k != "add_columns"
                    and not (k in ("where", "keys", "key") and (v is None or _blank_text(v)))
                }
                if _blank(load.get("mode")):
                    load["mode"] = "overwrite"
                target = {**target, "load": load}
        written.append(target)
    data["targets"] = written
    return data


def _blank_text(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


# Parts of a check the model may write empty although the format leaves them out: they
# mean "none" either way.
_OPTIONAL_TEXTS = ("where", "expected_where")
_OPTIONAL_LISTS = ("group_by",)


def _tidy_checks(proposed: Any) -> Any:
    """The plan's checks with what the model wrote empty left out: a part that is null
    (every kind's fields written out, null where they do not apply), a blank condition
    and an empty ``group_by``, also inside a reconcile's sides. Anything else is checked
    as it is."""
    if not isinstance(proposed, list):
        return proposed
    return [_tidy_check(check) if isinstance(check, dict) else check for check in proposed]


def _tidy_check(check: dict[str, Any]) -> dict[str, Any]:
    tidy: dict[str, Any] = {}
    for key, value in check.items():
        if value is None:
            continue
        if key in _OPTIONAL_TEXTS and _blank_text(value):
            continue
        if key in _OPTIONAL_LISTS and value == []:
            continue
        if key in ("left", "right") and isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if v is not None and not (k == "where" and _blank_text(v))
            }
        tidy[key] = value
    return tidy


RULE_ID = re.compile(r"[^a-z0-9_]+")


def _tidy_rule_names(plan: EtlPlanV2, cleaned: list[str]) -> None:
    """Quality rule names the code can use, one per target: a lower-case identifier.
    One that is not becomes its lower-case letters, digits and underscores, or
    ``rule_<n>`` when nothing is left (a name in Chinese); a repeated one gets a number.
    A name never sends the plan back."""
    for target in plan.targets:
        valid = {r.name for r in target.quality_rules if IDENTIFIER.match(r.name)}
        used: set[str] = set()
        renamed: list[tuple[str, str]] = []
        for n, rule in enumerate(target.quality_rules, start=1):
            if IDENTIFIER.match(rule.name) and rule.name not in used:
                used.add(rule.name)
                continue
            base = RULE_ID.sub("_", rule.name.strip().lower()).strip("_")[:50]
            if not base:
                base = f"rule_{n}"
            elif not base[0].isalpha():
                base = f"rule_{base}"
            name, k = base, 2
            while name in used or (name in valid and name != rule.name):
                name, k = f"{base}_{k}", k + 1
            used.add(name)
            renamed.append((rule.name, name))
            rule.name = name
        if renamed:
            pairs = "、".join(f"「{old}」→ {new}" for old, new in renamed)
            cleaned.append(f"{target.name} 的质量规则名改成了代码能用的名字：{pairs}")


def _pyspark_for_filters(plan: EtlPlanV2, project: Mapping[str, Any], cleaned: list[str]) -> None:
    """A plan in SQL that reads a table with a project read filter is written in PySpark
    instead: only the PySpark code reads through the filter (``source()``)."""
    if plan.language != "sql":
        return
    filtered = {
        str(r.get("name")).lower()
        for r in project.get("references") or []
        if isinstance(r, dict) and r.get("name") and r.get("filter")
    }
    read = [s for t in plan.targets for s in t.sources if s.lower() in filtered]
    if read:
        plan.language = "pyspark"
        cleaned.append(
            f"{'、'.join(dict.fromkeys(read))} 有项目的读取筛选，SQL 代码暂不支持，"
            "代码语言改成了 PySpark"
        )


def _param_problems(text: Any, where: str, project: Mapping[str, Any] | None) -> list[str]:
    """Run parameters in ``text`` the run has none of."""
    if not isinstance(text, str):
        return []
    keys = list(dict.fromkeys(PARAM.findall(text)))
    if not keys:
        return []
    if project is None:
        return [f"{where} 用了运行参数 ${{param.{keys[0]}}}，{UNBOUND}"]
    defined = {p.get("key") for p in project.get("params") or [] if isinstance(p, dict)}
    name = project.get("name") or project.get("id")
    return [
        f"{where} 的运行参数 ${{param.{key}}} 不是项目「{name}」定义的参数"
        for key in keys
        if key not in defined
    ]


def _condition_problems(text: str, where: str, label: str) -> list[str]:
    problem = filter_problem(text)
    if problem is not None:
        return [f"{where}：{problem.replace('读取筛选', label)}"]
    if "${" in PARAM.sub("", text):
        return [f"{where}：运行参数要写成 ${{param.<key>}}（key 是项目参数的标识）"]
    return []


def _load_problems(target: Target, project: Mapping[str, Any] | None) -> list[str]:
    """How the target is written (docs/api/data-nodes.md, 写表方式)."""
    load, name = target.load, target.name
    problems: list[str] = []
    where = (load.where or "").strip()
    if load.mode == "replace_where":
        if not where:
            problems.append(f"{name} 按条件替换（replace_where），要给出替换条件 load.where")
        else:
            problems += _condition_problems(where, f"{name} 的 load.where", "替换条件")
            problems += _param_problems(where, f"{name} 的 load.where", project)
    elif where:
        problems.append(f"{name} 的 load.where 只用在按条件替换（replace_where）")
    columns = {c.name for c in target.columns}
    if load.mode == "merge":
        if not (load.keys or target.primary_key):
            problems.append(f"{name} 按键合并（merge），要给出合并键 load.keys 或主键 primary_key")
        for key in dict.fromkeys(load.keys):
            if key not in columns:
                problems.append(f"{name} 的合并键 {key!r} 不在目标列里")
            if load.keys.count(key) > 1:
                problems.append(f"{name} 的合并键 {key!r} 重复")
    elif load.keys:
        problems.append(f"{name} 的 load.keys 只用在按键合并（merge）")
    if load.key is not None and load.key.strip():
        problems.append(
            f"{name} 的 load.key 在这一版不用：按条件替换写 load.where，按键合并写 load.keys"
        )
    return problems


def _project_problems(plan: EtlPlanV2, project: Mapping[str, Any]) -> list[str]:
    """The project's table prefix, and no target named like a table it reads."""
    problems = []
    prefix = project.get("table_prefix") or ""
    label = project.get("name") or project.get("id")
    read = {
        str(r.get("name") or "").rsplit(".", 1)[-1].lower(): str(r.get("name"))
        for r in project.get("references") or []
        if isinstance(r, dict) and r.get("name")
    }
    for target in plan.targets:
        if prefix and not target.name.startswith(prefix):
            problems.append(f"目标表名 {target.name!r} 要以项目「{label}」的表名前缀 {prefix} 开头")
        same = read.get(target.name.lower())
        if same is not None:
            problems.append(f"目标表名 {target.name!r} 和项目引用的表 {same} 同名，请换一个")
    return problems


def _plan_checks(
    proposed: Any, plan: EtlPlanV2, project: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """The checks the plan proposes, each one valid (with its ``severity``), and what is
    wrong with them."""
    if proposed is None:
        return [], []
    if not isinstance(proposed, list):
        return [], ["checks 应该是列表"]
    problems: list[str] = []
    if len(proposed) > MAX_PLAN_CHECKS:
        problems.append(f"checks 最多 {MAX_PLAN_CHECKS} 条，现在有 {len(proposed)} 条")
    tables = {t.name.lower() for t in plan.targets}
    tables |= {s.lower() for t in plan.targets for s in t.sources}
    # The project's acceptance checks run in every data check (include
    # {{project.checks}}); a plan check with the same id would clash there.
    owned = {c.get("id") for c in (project or {}).get("checks") or [] if isinstance(c, dict)}
    ids: set[str] = set()
    kept: list[dict[str, Any]] = []
    for i, check in enumerate(proposed):
        at = f"checks[{i}]"
        found = checks.problems(check)
        problems += [f"{_path(['checks', i, *map(str, loc)])}：{msg}" for loc, msg in found]
        if not isinstance(check, dict):
            continue
        wrong = bool(found)
        if check.get("kind") == checks.GOLDEN:
            wrong = True
            problems.append(
                f"{at}.kind：方案里的检查只能是 {'、'.join(PLAN_CHECK_KINDS)}；"
                "和老系统的逐行对比由数据检查节点加"
            )
        cid = check.get("id")
        if cid == checks.GOLDEN:
            wrong = True
            problems.append(f"{at}.id：id golden 留给数据检查节点和老系统的对比，请换一个")
        elif isinstance(cid, str) and cid in ids:
            wrong = True
            problems.append(f"{at}.id：检查 id {cid} 重复")
        elif isinstance(cid, str) and cid in owned:
            wrong = True
            problems.append(
                f"{at}.id：{cid} 是项目的验收检查，数据检查时会自动跑，方案里不用再写；"
                "要另加检查请换一个 id"
            )
        if isinstance(cid, str):
            ids.add(cid)
        for where, table in checks.tables(check):
            if where == "expected":
                continue  # golden, refused above
            if isinstance(table, str) and table.strip() and table.strip().lower() not in tables:
                wrong = True
                problems.append(
                    f"{at}.{where}：{table} 不是方案的目标表或来源，检查只能读方案写的表和读的源表"
                )
        for where, text in checks.texts(check):
            more = _param_problems(text, f"{at}.{where}", project)
            wrong = wrong or bool(more)
            problems += more
        if not wrong:
            # A check the model proposes only warns unless the plan says it must hold
            # (jadeflow acceptance I-10): a wrong model check at error level blocks a
            # right result.
            kept.append({**check, "severity": check.get("severity") or "warn"})
    return kept, problems


# The write modes that keep an existing table's rows, and so its columns.
KEEPING_MODES = ("replace_where", "append", "merge")
KEEPING_NAMES = {"replace_where": "按条件替换", "append": "追加", "merge": "按键合并"}


def existing_columns(table: Mapping[str, Any]) -> list[str]:
    """The column names of an existing table as the profile node lists it."""
    found = table.get("columns")
    listed = found if isinstance(found, list) else []
    return [
        str(c["name"])
        for c in listed
        if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"]
    ]


def existing_table(
    target: Target, existing: Sequence[Mapping[str, Any]] | None
) -> Mapping[str, Any] | None:
    """The project's existing table ``target`` writes to (names are not case sensitive)."""
    for table in existing or []:
        if isinstance(table, Mapping) and str(table.get("name") or "").lower() == (
            target.name.lower()
        ):
            return table
    return None


def _schema_problems(plan: EtlPlanV2, existing: Sequence[Mapping[str, Any]]) -> list[str]:
    """Sets each target's ``load.add_columns`` from the table it writes to, and says
    what is wrong (docs/api/data-nodes.md, 表结构变化): a write that keeps the table's
    rows writes every column it has; the plan's columns it lacks are added where the plan
    puts them. Overwriting rebuilds the table as the plan says, so nothing is added."""
    problems: list[str] = []
    for target in plan.targets:
        target.load.add_columns = []
        table = existing_table(target, existing)
        if table is None or target.load.mode not in KEEPING_MODES:
            continue
        have = existing_columns(table)
        lower = {c.lower() for c in have}
        planned = {c.name.lower() for c in target.columns}
        missing = [c for c in have if c.lower() not in planned]
        if missing:
            problems.append(
                f"{target.name} 已经存在，表里的列 {'、'.join(missing)} 不在方案的 columns 里："
                f"{KEEPING_NAMES[target.load.mode]}会保留表里其他的行，要写出表里已有的全部列，"
                "新加的列写在想放的位置；要去掉列或重建表，请用 overwrite"
            )
        after: str | None = None
        for column in target.columns:
            if column.name.lower() not in lower:
                target.load.add_columns.append(AddColumn(name=column.name, after=after))
            after = column.name
    return problems


def _version_three_checks(
    plan: EtlPlanV2,
    proposed: Any,
    project: Mapping[str, Any] | None,
    existing: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    problems: list[str] = []
    for target in plan.targets:
        problems += _load_problems(target, project)
    if project is not None:
        problems += _project_problems(plan, project)
    if existing is not None:
        problems += _schema_problems(plan, existing)
    kept, more = _plan_checks(proposed, plan, project)
    return kept, problems + more


# The platform's part of the plan node's prompt for version 3: the JSON it parses. How
# to write such a plan is version 2 of the ``etl-plan-brief`` skill.
OUTPUT_FORMAT_V3 = """只输出一个 JSON 对象，不要输出其他文字，结构如下：
{
  "summary": "一两句话说明方案",
  "language": "pyspark 或 sql",
  "brief": {
    "goal": "要统计什么，一两句话",
    "grain": "结果表一行是什么，如“每个统计月 × 城市一行”",
    "metrics": [{"id": "m1", "name": "指标名称", "unit": "单位，没有时为 null",
                 "definition": "怎么算，用文字写", "column": "结果表里的列名"}],
    "dimensions": [{"id": "d1", "name": "维度名称", "definition": "取自哪里、怎么归一",
                    "column": "结果表里的列名", "examples": ["数据里真有的取值，最多 5 个"]}],
    "filters": [{"id": "f1", "text": "筛选条件"}],
    "time_range": {"text": "时间范围和按哪个时间算", "column": "结果表里的时间列"}
  },
  "targets": [
    {
      "name": "目标表名（绑定了项目时以项目的表名前缀开头）",
      "description": "这张表是什么",
      "sources": ["源表名，照探查结果里的写", "也可以引用前面的目标表名"],
      "logic": "关联、过滤、聚合、清洗的做法",
      "columns": [{"name": "列名", "label": "给人看的列名，如“销售额（元）”",
                   "source": "Spark SQL 表达式", "description": ""}],
      "primary_key": ["列名"],
      "load": {"mode": "overwrite、replace_where、append 或 merge",
               "where": "replace_where 时替换哪些行（Spark SQL 条件），其他写法为 null",
               "keys": ["merge 时的合并键，空着时用 primary_key"]},
      "quality_rules": [{"name": "规则名", "condition": "Spark SQL 布尔表达式",
                         "on_violation": "warn、drop 或 fail"}]
    }
  ],
  "rules": [{"id": "r1", "text": "已经定下的处理，一条一句",
             "basis": "knowledge、request、data、assumption 或 confirmed"}],
  "questions": [{"id": "q1", "text": "要人拍板的问题，最多 50 个字", "why": "为什么问",
                 "options": [{"value": "选项值", "label": "选项名称"}],
                 "suggested": "方案现在按哪个选项写（选项的 value）", "affects": ["m1"]}],
  "answer": {"target": "结果表名", "filter": [{"column": "时间列", "pick": "latest"}],
             "group_by": ["分组列，最多 3 个"], "measures": ["指标列，1–8 个"],
             "order_by": [{"column": "排序列", "desc": true}], "top_n": 10,
             "chart": {"type": "bar、line 或 none", "label": ["分组列"], "value": "指标列"},
             "add_up": ["可以跨行相加、但不是 sum 或 count 算出来的指标列，没有就给空数组"]},
  "checks": [{"id": "c1", "label": "检查名称，给人看",
              "kind": "aggregate、reconcile、unique 或 rows",
              "severity": "warn、error 或 info，不写时是 warn", "其余字段": "按检查的种类，见下"}]
}
不限时间时 time_range 给 null；没有要问的 questions 给空数组，最多 5 题，每题 2–6 个选项。
load.mode：overwrite 整表覆盖；replace_where 只替换满足 where 的行（如一个月、一个版本）；
append 追加；merge 按键合并（有的更新，没有的插入）。
checks 是建议在开发环境试算后做的数据检查，最多 10 条，没有就给空数组；
表名只能是上面的目标表名和 sources 里的源表。severity：warn 不通过时只提醒；error 不通过时结果
不能直接通过，只给确定了结果一错就不能交付的检查用；info 只列出来给人看。各种检查的其余字段：
- aggregate（一个聚合值和期望比）：
  {"table": "表名", "expr": "一个聚合表达式，如 sum(qty)", "where": "条件或 null",
   "group_by": ["可选：按组算，每组都要满足"], "op": "eq、ne、gt、ge、lt、le 或 between",
   "value": 比较的值, "max": "between 时的上限，其他为 null", "tolerance": 0}
- reconcile（两张表的聚合值对账）：
  {"left": {"table": "表名", "expr": "sum(qty)", "where": null},
   "right": {"table": "表名", "expr": "sum(volume)", "where": null},
   "group_by": ["可选，两边同名的分组列"], "tolerance": 0.005, "relative": true}
- unique（键不重复）：{"table": "表名", "columns": ["键列"], "where": null}
- rows（满足条件的行必须没有）：{"table": "表名", "where": "不该出现的行满足的条件"}
load.where 和检查的 where、expr 里可以用 ${param.参数标识} 引用项目的运行参数，
运行时换成参数值；没绑定项目时不能用。
和老系统结果的逐行对比由数据检查节点自动加，不要写在 checks 里。"""
