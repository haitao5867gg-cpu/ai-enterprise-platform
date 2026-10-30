"""The ETL plan: what the model proposes and the user confirms before any code exists.

The model answers in JSON matching ``EtlPlan``; ``validate_plan`` then checks it
against the profiled source tables, so a plan that names a table or column that
does not exist goes back to the model instead of on to the user.

Version 2 (``validate_plan_v2``, the flow node etl_plan_check with ``format`` 2) adds a
business brief, rules, questions for a person and an answer design; the platform tidies
what it can and sends the rest back the same way.
"""

import json
import keyword
import re
import unicodedata
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, PrivateAttr, TypeAdapter, ValidationError, field_validator

from aep.etl import answer
from aep.etl.answer import AnswerDesign
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
            # '' is a blank check (trim(city) = ''), not a value the model made up.
            if seen is None or not _normal(literal) or _normal(literal) in seen.values:
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


def _structure(exc: ValidationError, where: str = "") -> list[str]:
    problems = []
    for error in exc.errors(include_url=False):
        kind, ctx = error["type"], error.get("ctx") or {}
        if kind == "missing" or error.get("input", "") is None:
            what = "不能为空"
        elif kind == "literal_error":
            what = "只能是 " + "、".join(re.findall(r"'([^']*)'", str(ctx.get("expected"))))
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


def _parse_v2(raw: dict[str, Any]) -> tuple[EtlPlanV2 | None, list[str]]:
    """The plan, and what is wrong with its structure. Each of ``brief``, ``rules``,
    ``questions`` and ``answer`` is read on its own: a broken one is left out (and not
    generated in its place) so the rest can still be checked. The old ``assumptions``
    become rules with ``basis`` assumption, after the model's own rules."""
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
        plan = EtlPlanV2.model_validate({**data, "assumptions": [], "references": []})
    except ValidationError as exc:
        problems += _structure(exc)
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
        validate_plan(projection, source_tables, known, trust_assumptions)
    except PlanError as exc:
        problems += exc.problems
    return problems


def _version_two_checks(plan: EtlPlanV2) -> list[str]:
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
        problems += answer.design_problems(plan.answer, targets)
    return problems


def validate_plan_v2(
    raw: Any,
    source_tables: Collection[str],
    known: Mapping[str, KnownValues] | None = None,
    seen: Seen | None = None,
    trust_assumptions: bool = True,
    references: list[IndexedHit] | None = None,
    answered: Mapping[str, Confirmed] | None = None,
) -> tuple[EtlPlanV2, list[str]]:
    """Parse and tidy a version 2 plan, then check it like ``validate_plan`` and more.

    ``seen`` holds every value the profile showed per table and column name (``aep.etl.
    profile.seen_values``), for a dimension's examples; ``answered`` the answers people gave, by
    ``bare`` question text. Returns the plan and what the platform tidied (``cleaned``);
    raises ``PlanError`` with what the model has to rewrite."""
    if not isinstance(raw, dict):
        raise PlanError("方案要是一个 JSON 对象")
    model_brief = raw.get("brief") is not None
    model_answer = raw.get("answer") is not None
    plan, structure = _parse_v2(raw)
    if plan is None:
        raise PlanError("；".join(structure), structure)
    cleaned: list[str] = []
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
    problems = _version_one_checks(plan, source_tables, known, trust_assumptions)
    problems += structure + _blank_texts(plan) + _version_two_checks(plan)
    if problems:
        unique = list(dict.fromkeys(problems))
        raise PlanError("；".join(unique), unique)
    plan.assumptions = []
    if plan.answer is not None:
        plan.answer = answer.with_defaults(plan.answer)
    return plan, cleaned


def uncited(plan: EtlPlanV2) -> list[str]:
    """Ids of the metrics, dimensions and filters whose words cite no source."""
    if plan.brief is None:
        return []
    texts = [(m.id, m.definition) for m in plan.brief.metrics]
    texts += [(d.id, d.definition) for d in plan.brief.dimensions]
    texts += [(f.id, f.text) for f in plan.brief.filters]
    return [item_id for item_id, text in texts if not CITATION.search(text)]


def answer_output(plan: EtlPlanV2) -> dict[str, Any] | None:
    """The answer design with its columns and sentence (docs/api/data-nodes.md)."""
    if plan.answer is None:
        return None
    table = _result(plan)
    sources = {c.name: c.source for c in table.columns}
    labels = {c.name: _label_or_name(c) for c in table.columns}
    units: dict[str, str | None] = {}
    names = dict(labels)
    if plan.brief is not None:
        for d in reversed(plan.brief.dimensions):
            if d.column is not None:
                names[d.column] = d.name
        for m in reversed(plan.brief.metrics):
            if m.column is not None:
                names[m.column] = m.name
                units[m.column] = None if _blank(m.unit) else m.unit
    return plan.answer.model_dump() | {
        "columns": answer.columns(plan.answer, sources, labels, units),
        "text": answer.text(plan.answer, names),
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
             "chart": {"type": "bar、line 或 none", "label": ["分组列"], "value": "指标列"}}
}
不限时间时 time_range 给 null；没有要问的 questions 给空数组，最多 5 题，每题 2–6 个选项。"""
