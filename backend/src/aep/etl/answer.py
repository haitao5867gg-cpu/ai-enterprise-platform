"""The answer card a version 2 plan ends with (docs/api/data-nodes.md, "答案设计和可加性").

The model designs which result table, groups, measures, order and chart the card shows.
Whether a measure adds up across groups (``agg``) is decided here from the result
table's ``source`` expressions and is never taken from the model. The numbers are
worked out on the cluster (``aep.cluster_harness``, ``aep.cluster_tasks``) from the
design sent as data (``cluster_design``); the column formats, the conclusion and the
query the card shows are written here (``result``), never by a model ("答案怎么算").
"""

import json
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, StrictInt, field_validator

Agg = Literal["sum", "ratio", "none"]
CHARTS = ("bar", "line", "none")
MAX_GROUP_BY = 3
MAX_MEASURES = 8
MAX_TOP_N = 50
TOP_N = 10


class OrderBy(BaseModel):
    column: str
    desc: bool = True


class Chart(BaseModel):
    type: str | None = None
    label: list[str] | None = None
    value: str | None = None


class AnswerDesign(BaseModel):
    target: str
    # The first stage knows one filter only: {"column": ..., "pick": "latest"}.
    filter: list[Any] = []
    group_by: list[str] = []
    measures: list[str]
    order_by: list[OrderBy] | None = None
    # Strict: true or "10" is not a number of groups.
    top_n: StrictInt | None = None
    chart: Chart | None = None

    @field_validator("filter", "group_by", mode="before")
    @classmethod
    def _null_is_empty(cls, value: Any) -> Any:
        return [] if value is None else value


def _latest(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and set(item) == {"column", "pick"}
        and isinstance(item["column"], str)
        and item["pick"] == "latest"
    )


def design_problems(design: AnswerDesign, targets: Mapping[str, Sequence[str]]) -> list[str]:
    """What is wrong with ``design``; ``targets`` are the plan's target tables (name to
    column names, in order). A target that is not one is checked against the last."""
    problems = []
    table = design.target
    if table not in targets:
        problems.append(f"answer.target {table} 不是目标表")
        table = list(targets)[-1]
    columns = set(targets[table])

    def outside(where: str, names: Sequence[str]) -> list[str]:
        return [f"answer.{where} 的列 {c} 不在结果表 {table} 里" for c in names if c not in columns]

    if len(design.group_by) > MAX_GROUP_BY:
        problems.append(f"answer.group_by 最多 {MAX_GROUP_BY} 列")
    if not 1 <= len(design.measures) <= MAX_MEASURES:
        problems.append(f"answer.measures 要有 1–{MAX_MEASURES} 列")
    problems += outside("group_by", design.group_by)
    problems += outside("measures", design.measures)
    for where, names in (("group_by", design.group_by), ("measures", design.measures)):
        twice = [c for c in dict.fromkeys(names) if names.count(c) > 1]
        problems += [f"answer.{where} 的列 {c} 重复" for c in twice]
    picked = set(design.group_by) | set(design.measures)
    for order in design.order_by or []:
        if order.column not in columns:
            problems += outside("order_by", [order.column])
        elif order.column not in picked:
            problems.append(f"answer.order_by 的列 {order.column} 要在 group_by 或 measures 里")
    if design.filter:
        if len(design.filter) > 1 or not _latest(design.filter[0]):
            problems.append('answer.filter 第一阶段只能写一条 {"column": 列名, "pick": "latest"}')
        else:
            problems += outside("filter", [design.filter[0]["column"]])
    if design.top_n is not None and not 1 <= design.top_n <= MAX_TOP_N:
        problems.append(f"answer.top_n 要在 1–{MAX_TOP_N} 之间")
    chart = design.chart or Chart()
    if chart.type is not None and chart.type not in CHARTS:
        problems.append("answer.chart.type 只能是 bar、line 或 none")
    if chart.value is not None and chart.value not in design.measures:
        problems.append(f"answer.chart.value {chart.value} 要在 measures 里")
    for c in chart.label or []:
        if c not in design.group_by:
            problems.append(f"answer.chart.label 的列 {c} 要在 group_by 里")
    return problems


def with_defaults(design: AnswerDesign) -> AnswerDesign:
    """``design`` with what the model may leave out filled in."""
    chart = design.chart or Chart()
    first = design.measures[0]
    return design.model_copy(
        update={
            "order_by": design.order_by or [OrderBy(column=first, desc=True)],
            "top_n": TOP_N if design.top_n is None else design.top_n,
            "chart": Chart(
                type=chart.type or ("bar" if design.group_by else "none"),
                label=list(design.group_by) if chart.label is None else chart.label,
                value=chart.value or first,
            ),
        }
    )


def generate(
    table: str,
    columns: Collection[str],
    dimensions: Sequence[str],
    metrics: Sequence[str],
    time_column: str | None,
) -> AnswerDesign | None:
    """A design from the brief, for a plan whose model wrote none: the latest period of
    ``time_column``, the first dimensions as groups and the first metrics as measures
    (all of them columns of ``table``). None without a metric."""
    measures = list(dict.fromkeys(metrics))[:MAX_MEASURES]
    if not measures:
        return None
    latest = time_column is not None and time_column in columns
    group_by = [d for d in dict.fromkeys(dimensions) if not (latest and d == time_column)]
    return with_defaults(
        AnswerDesign(
            target=table,
            filter=[{"column": time_column, "pick": "latest"}] if latest else [],
            group_by=group_by[:MAX_GROUP_BY],
            measures=measures,
        )
    )


# Whether a measure adds up: read the result column's source expression.

CALL = re.compile(r"([A-Za-z_]\w*)\s*\(")
COLUMN_REF = re.compile(r"(?:`?\w+`?\.)*`?(\w+)`?")
ZERO = re.compile(r"0(?:\.0+)?")
DISTINCT = re.compile(r"distinct\b", re.I)
AS = re.compile(r"\s+as\s+", re.I)


def _mask(expr: str) -> str:
    """``expr`` with everything in quotes or parentheses (and those) blanked out."""
    out = []
    depth, quote = 0, ""
    for ch in expr:
        hidden = bool(quote) or depth > 0
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote, hidden = ch, True
        elif ch == "(":
            depth, hidden = depth + 1, True
        elif ch == ")":
            depth, hidden = depth - 1, True
        out.append("\0" if hidden else ch)
    return "".join(out)


def _closing(expr: str, start: int) -> int | None:
    """Where the parenthesis that opens at ``start`` closes."""
    depth, quote = 0, ""
    for i in range(start, len(expr)):
        ch = expr[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _split(text: str, at: str = ",") -> list[str]:
    """``text`` split at ``at`` outside quotes and parentheses."""
    if not text.strip():
        return []
    cuts = [i for i, ch in enumerate(_mask(text)) if ch == at]
    bounds = [-1, *cuts, len(text)]
    return [text[a + 1 : b].strip() for a, b in zip(bounds, bounds[1:], strict=False)]


def _call(expr: str) -> tuple[str, list[str]] | None:
    """``name(args)`` when the call is the whole of ``expr``: ``sum(x) OVER (…)`` is not."""
    match = CALL.match(expr)
    if not match or _closing(expr, match.end() - 1) != len(expr) - 1:
        return None
    return match[1].lower(), _split(expr[match.end() : -1])


def _unwrap(expr: str) -> str:
    """``expr`` without the wrappers that keep its value: parentheses, ``round``,
    ``bround``, ``cast``/``try_cast``, and ``coalesce``/``nvl``/``ifnull`` with 0."""
    while True:
        expr = expr.strip()
        if expr.startswith("(") and _closing(expr, 0) == len(expr) - 1:
            expr = expr[1:-1]
            continue
        call = _call(expr)
        if call is None:
            return expr
        name, args = call
        if name in ("round", "bround") and len(args) in (1, 2):
            expr = args[0]
        elif name in ("cast", "try_cast") and len(args) == 1:
            found = list(AS.finditer(_mask(args[0])))
            if not found:
                return expr
            expr = args[0][: found[-1].start()]
        elif name in ("coalesce", "nvl", "ifnull") and len(args) == 2 and ZERO.fullmatch(args[1]):
            expr = args[0]
        else:
            return expr


def _sum_like(expr: str) -> bool:
    """``sum(x)``, ``count(*)``, ``count(1)``, ``count(column)`` (no DISTINCT) or
    ``count_if(…)``: a value that adds up across groups."""
    call = _call(expr)
    if call is None:
        return False
    name, args = call
    if name == "count_if":
        return True
    if len(args) != 1 or DISTINCT.match(args[0]):
        return False
    if name == "sum":
        return True
    return name == "count" and (args[0] in ("*", "1") or bool(COLUMN_REF.fullmatch(args[0])))


def _normal(expr: str) -> str:
    """``expr`` as compared for sameness: spaces and case count only where they are part
    of a name or a value. A string literal keeps its exact text ('web shop' is not
    'webshop'); a backticked name drops its backticks and case but keeps its spaces
    (`net sales` is not netsales, `line_amount` is line_amount)."""
    out, quote = [], ""
    for ch in expr:
        if quote == "`":
            if ch == "`":
                quote = ""
            else:
                out.append(ch.lower())
        elif quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
            if ch != "`":
                out.append(ch)
        elif not ch.isspace():
            out.append(ch.lower())
    return "".join(out)


def _sum_column(name: str, side: str, sources: Mapping[str, str], denominator: bool) -> str | None:
    """The ``sum`` column of the table that one side of a division is: its name, or an
    expression the same as its source as written (wrappers there are kept)."""
    expr = _unwrap(side)
    call = _call(expr)
    if denominator and call and call[0] == "nullif" and len(call[1]) == 2:
        if ZERO.fullmatch(call[1][1]):
            expr = _unwrap(call[1][0])
    ref = COLUMN_REF.fullmatch(expr)
    for column, source in sources.items():
        if column == name or not _sum_like(_unwrap(source)):
            continue
        if ref and ref[1].lower() == column.lower():
            return column
        if _normal(expr) == _normal(source):
            return column
    return None


def additivity(name: str, sources: Mapping[str, str]) -> tuple[Agg, str | None, str | None]:
    """``agg`` of column ``name`` with its numerator and denominator columns for a ratio;
    ``sources`` maps the result table's columns to their source expressions."""
    if name not in sources:
        return "none", None, None
    expr = _unwrap(sources[name])
    if _sum_like(expr):
        return "sum", None, None
    call = _call(expr)
    if call and call[0] == "try_divide" and len(call[1]) == 2:
        sides: list[str] | None = call[1]
    else:
        slashes = [i for i, ch in enumerate(_mask(expr)) if ch == "/"]
        sides = [expr[: slashes[0]], expr[slashes[0] + 1 :]] if len(slashes) == 1 else None
    if sides:
        numerator = _sum_column(name, sides[0], sources, False)
        denominator = _sum_column(name, sides[1], sources, True)
        if numerator and denominator:
            return "ratio", numerator, denominator
    return "none", None, None


BRACKET = re.compile(r"[（(]([^（）()]+)[）)]\s*$")


def columns(
    design: AnswerDesign,
    sources: Mapping[str, str],
    labels: Mapping[str, str],
    units: Mapping[str, str | None],
) -> list[dict[str, Any]]:
    """The card's columns: the groups, then the measures with ``agg`` and ``unit`` (the
    brief's unit, else the bracket at the end of the label). No ``format`` yet: a
    column's type is known only after it runs."""
    out: list[dict[str, Any]] = [
        {"name": c, "label": labels.get(c) or c, "role": "dimension"} for c in design.group_by
    ]
    for c in design.measures:
        label = labels.get(c) or c
        agg, numerator, denominator = additivity(c, sources)
        bracket = BRACKET.search(label)
        unit = units.get(c) or (bracket[1].strip() if bracket else None) or None
        item: dict[str, Any] = {
            "name": c,
            "label": label,
            "role": "metric",
            "agg": agg,
            "unit": unit,
        }
        if agg == "ratio":
            item |= {"numerator": numerator, "denominator": denominator}
        out.append(item)
    return out


CHART_WORDS = {"bar": "和柱状图", "line": "和折线图"}


def text(design: AnswerDesign, names: Mapping[str, str]) -> str:
    """The sentence the plan review shows under "最后给你的答案"; ``design`` has its
    defaults (``with_defaults``), ``names`` are what the brief calls the columns."""

    def name(column: str) -> str:
        return names.get(column) or column

    first = design.filter[0] if design.filter else None
    time = name(first["column"]) if isinstance(first, dict) else None
    if not design.group_by:
        measures = "、".join(name(c) for c in design.measures)
        return f"最近一个{time}的{measures}合计。" if time else f"{measures}合计。"
    head = f"最近一个{time}里，" if time else ""
    groups = " × ".join(name(c) for c in design.group_by)
    order = (design.order_by or [OrderBy(column=design.measures[0])])[0]
    way = "从高到低" if order.desc else "从低到高"
    chart = CHART_WORDS.get((design.chart or Chart()).type or "", "")
    return (
        f"{head}各「{groups}」组合按{name(order.column)}{way}排的前 {design.top_n} 组，"
        f"附合计{chart}。"
    )


# The answer worked out on the cluster (docs/api/data-nodes.md, "答案怎么算").

INTEGER_TYPES = ("tinyint", "smallint", "int", "bigint")
TIME_LITERALS = {"date": "DATE", "timestamp": "TIMESTAMP", "timestamp_ntz": "TIMESTAMP_NTZ"}


def cluster_design(card: Mapping[str, Any]) -> dict[str, Any]:
    """What the cluster needs of ``card`` (the plan check's ``answer``) to work it out."""
    first = card["filter"][0] if card.get("filter") else None
    measures = []
    for c in card["columns"]:
        if c["role"] != "metric":
            continue
        measure = {"name": c["name"], "agg": c["agg"]}
        if c["agg"] == "ratio":
            measure |= {"numerator": c["numerator"], "denominator": c["denominator"]}
        measures.append(measure)
    return {
        "target": card["target"],
        "filter": first["column"] if first else None,
        "group_by": list(card["group_by"]),
        "measures": measures,
        "order_by": [{"column": o["column"], "desc": o["desc"]} for o in card["order_by"]],
        "top_n": card["top_n"],
    }


def column_format(kind: str | None) -> str | None:
    """``integer`` or ``decimal`` for a number column of the Spark type ``kind``."""
    if kind in INTEGER_TYPES:
        return "integer"
    if kind in ("float", "double") or (kind or "").startswith("decimal"):
        return "decimal"
    return None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def shown(value: Any, fmt: str | None = None) -> str:
    """A value as the card writes it: a number by its column's format, never by whether
    the value has a decimal point (the browser cannot tell 1234567.0 from 1234567)."""
    if value is None:
        return "—"
    if _number(value) and fmt == "integer":
        return f"{value:,}" if isinstance(value, int) else f"{value:,.0f}"
    if _number(value) and fmt == "decimal":
        return f"{value:,.2f}"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _joined(head: str, tail: str) -> str:
    """``head`` then ``tail``, a space apart where a letter or digit meets Chinese."""
    space = head and tail and head[-1].isascii() and head[-1].isalnum() and not tail[0].isascii()
    return f"{head} {tail}" if space else head + tail


def _metric_name(column: Mapping[str, Any], names: Mapping[str, str]) -> str:
    if names.get(column["name"]):
        return names[column["name"]]
    return BRACKET.sub("", column["label"]).strip() or column["label"]


def conclusion(answer: Mapping[str, Any], card: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """The sentence the card leads with; ``answer`` is worked out, ``names`` are what the
    brief calls the columns."""
    period = answer["period"] or ""
    columns = {c["name"]: c for c in answer["columns"]}
    order = card["order_by"][0]
    group_by = card["group_by"]

    def amount(column: str, value: Any) -> str:
        unit = columns[column].get("unit")
        text = shown(value, columns[column].get("format"))
        return f"{text} {unit}" if unit and value is not None else text

    def lead(text: str) -> str:
        return f"{period}，{text}" if period else text

    if answer["groups"] == 0:
        return _joined(period, "没有符合条件的数据。")
    if not group_by:
        metric = columns[order["column"]]
        value = answer["rows"][0].get(order["column"])
        return lead(f"{_metric_name(metric, names)}合计：{amount(order['column'], value)}。")
    if order["column"] in group_by:
        column = columns[order["column"]]
        return _joined(
            period,
            f"共 {answer['groups']} 组，按{names.get(column['name']) or column['label']}排序。",
        )
    row = answer["rows"][0]
    metric = columns[order["column"]]
    value = row.get(metric["name"])
    who = " · ".join(shown(row.get(c)) for c in group_by)
    said = _joined(who, f"的{_metric_name(metric, names)}{'最高' if order['desc'] else '最低'}")
    said += f"：{amount(metric['name'], value)}"
    total = answer["totals"].get(metric["name"])
    least = answer["mins"].get(metric["name"])
    if (
        metric["agg"] == "sum"
        and _number(value)
        and _number(least)
        and least >= 0
        and _number(total)
        and total > 0
    ):
        said += f"，占合计 {value / total * 100:.1f}%"
    return lead(said + "。")


def _name(*parts: str) -> str:
    return ".".join("`" + part.replace("`", "``") + "`" for part in parts)


def _literal(text: str, kind: str | None) -> str:
    """The period as a Spark SQL literal of its column's type ``kind``."""
    if column_format(kind):
        return text
    quoted = "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    typed = TIME_LITERALS.get(kind or "")
    return f"{typed} {quoted}" if typed else quoted


def query(
    card: Mapping[str, Any], table: Sequence[str], period: str | None, kind: str | None
) -> str:
    """The query that gives the same answer, for a person to copy (it is never run);
    ``period`` is the latest value of the filter column (of type ``kind``), when known."""
    q = _name
    where = _name(*table)
    select = [q(c) for c in card["group_by"]]
    for c in card["columns"]:
        if c["role"] != "metric":
            continue
        name = q(c["name"])
        if c["agg"] == "sum":
            select.append(f"sum({name}) AS {name}")
        elif c["agg"] == "ratio":
            numerator, denominator = q(c["numerator"]), q(c["denominator"])
            select.append(f"sum({numerator}) / nullif(sum({denominator}), 0) AS {name}")
        else:
            select.append(f"CASE WHEN count(*) = 1 THEN first({name}) END AS {name}")
    lines = ["SELECT " + ", ".join(select), f"FROM {where}"]
    if card.get("filter"):
        column = q(card["filter"][0]["column"])
        value = (
            _literal(period, kind)
            if period is not None
            else (f"(SELECT max({column}) FROM {where})")
        )
        lines.append(f"WHERE {column} = {value}")
    if card["group_by"]:
        lines.append("GROUP BY " + ", ".join(q(c) for c in card["group_by"]))
    ordering = [
        f"{q(o['column'])} {'DESC' if o['desc'] else 'ASC'} NULLS LAST" for o in card["order_by"]
    ]
    ordering += [f"{q(c)} ASC NULLS FIRST" for c in card["group_by"]]
    lines += ["ORDER BY " + ", ".join(ordering), f"LIMIT {card['top_n']}"]
    return "\n".join(lines)


def result(
    card: Mapping[str, Any],
    worked: Mapping[str, Any] | None,
    *,
    table: Sequence[str],
    target: str,
    quality: Any,
    url: str | None,
    names: Mapping[str, str],
) -> dict[str, Any]:
    """The ``answer`` output of a dev run: ``card`` is the plan check's answer, ``worked``
    what the cluster worked out (or ``{"error": ...}``), ``table`` the result table's
    name in parts and ``target`` as shown, ``names`` what the brief calls the columns."""
    worked = worked or {"error": "集群没有带回答案"}
    first = card["filter"][0]["column"] if card.get("filter") else None
    types = worked.get("types") or {}
    columns = []
    for c in card["columns"]:
        if worked.get("error") is not None or c["role"] != "metric":
            fmt = None
        else:
            fmt = "decimal" if c["agg"] == "ratio" else column_format(types.get(c["name"]))
        columns.append(dict(c) | {"format": fmt})
    out: dict[str, Any] = {
        "status": "error",
        "target": target,
        "period": None,
        "columns": columns,
        "rows": [],
        "totals": {},
        "mins": {},
        "groups": None,
        "row_count": None,
        "truncated": None,
        "conclusion": "",
        "chart": card["chart"],
        "quality": quality,
        "sql": query(card, table, None, None),
        "url": url,
        "error": worked.get("error"),
    }
    if worked.get("error") is not None:
        return out
    raw = worked.get("period")
    period = None if raw is None else raw if isinstance(raw, str) else json.dumps(raw)
    out |= {
        "status": "ok",
        "period": period,
        "rows": list(worked["rows"]),
        "totals": dict(worked["totals"]),
        "mins": dict(worked["mins"]),
        "groups": worked["groups"],
        "row_count": worked["row_count"],
        "truncated": worked["groups"] > card["top_n"],
        "sql": query(card, table, period, types.get(first) if first else None),
    }
    out["conclusion"] = conclusion(out, card, names)
    return out
