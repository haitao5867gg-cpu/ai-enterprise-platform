"""Data checks (docs/api/data-nodes.md, "data_check 数据检查"): what a check may say,
which tables and conditions it reads, and how its outcome is judged and reported.

The node ``data_check`` (aep.runtime.flow.data_check) runs the checks through a
Databricks data source's read-only connector: ``aggregate`` and ``reconcile`` through its
``aggregate`` tool, ``unique`` through ``duplicates``, ``golden`` through ``compare``, and
``rows`` through ``aggregate`` and ``sample_rows``. The connector reads; comparisons,
tolerances and conclusions are worked out here. Plan format 3 proposes checks in the same
shape, so it uses :func:`problems` too.
"""

import json
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from aep.cluster_harness import PARAM, with_params
from aep.projects import filter_problem
from aep.tools.mcp import ConnectorUnavailable, ToolError

KINDS = ("aggregate", "reconcile", "unique", "golden", "rows")
SEVERITIES = ("error", "warn", "info")
OPS = ("eq", "ne", "gt", "ge", "lt", "le", "between")
SIGN = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤"}
ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# The id of the comparison with the old system a node adds by itself.
GOLDEN = "golden"
GOLDEN_LABEL = "和老系统的结果一致"
# Checks written in a node, and all of a node's checks with those it includes.
MAX_CHECKS = 50
MAX_ALL = 100
# Groups one grouped check can compare, and examples a result keeps.
GROUP_LIMIT = 10_000
EXAMPLES = 20
MAX_EXPRESSION = 2000
MAX_GROUP_COLUMNS = 10
MAX_KEYS = 20
MAX_COMPARED = 200
# Known differences a check may list (jadeflow acceptance I-4), and what besides ``match``
# and ``note`` pins one down to a cell or a value (jadeflow retest I-16).
MAX_KNOWN = 20
KNOWN_FIELDS = {"match", "note"}
KNOWN_MORE = {"golden": ("columns",), "aggregate": ("value",), "reconcile": ("left", "right")}
KNOWN_SIDES = {"table", "expected"}
MAX_KNOWN_COLUMNS = 20
MAX_NOTE = 200
# Rows of known differences a golden check looks at one by one.
KNOWN_ROWS = 200
# A table name: a result table (bare), a source (schema.table) or catalog.schema.table.
_PART = r"[^\s`.]{1,255}"
TABLE = re.compile(rf"^{_PART}(?:\.{_PART}){{0,2}}$")
_COLUMN = re.compile(r"^[^`\x00-\x1f]{1,255}$")
# What the MCP client and server put before a tool's own error message.
TOOL_PREFIX = re.compile(r"^\s*(?:\w+: )?(?:Error executing tool \w+: )?")

COMMON = {"id", "label", "kind", "severity", "known", "why"}
# Why a check has its severity, above all why an error must block (jadeflow 4th retest
# I-10: a model check at error level without a reason blocked right results).
MAX_WHY = 500
FIELDS = {
    "aggregate": {"table", "expr", "where", "group_by", "op", "value", "max", "tolerance"},
    "reconcile": {"left", "right", "group_by", "tolerance", "relative"},
    "unique": {"table", "columns", "where"},
    "golden": {"table", "expected", "keys", "columns", "where", "expected_where", "tolerance"},
    "rows": {"table", "where", "lookup"},
}
SIDE = {"table", "expr", "where"}
# A rows check may keep only the rows with (or without) a matching row in another table.
LOOKUP = {"table", "on", "where", "found", "trim"}
MAX_LOOKUP_COLUMNS = 10

STATUS_TEXT = {"passed": "通过", "failed": "不通过", "error": "出错", "skipped": "跳过"}
SEVERITY_TEXT = {"error": "错误", "warn": "警告", "info": "提示"}
DIFFERENCE = {"missing": "少", "extra": "多", "different": "不一致", "duplicate": "重复"}
# Columns an example table in the report shows.
REPORT_COLUMNS = 10

Loc = tuple[str | int, ...]


# What a check may say


def problems(check: Any) -> list[tuple[Loc, str]]:
    """What is wrong with one check, with where inside it; [] when it may run."""
    found: list[tuple[Loc, str]] = []

    def add(loc: Loc, message: str) -> None:
        found.append((loc, message))

    if not isinstance(check, dict):
        return [((), "每条检查要写成一个对象")]
    cid = check.get("id")
    if not isinstance(cid, str) or not ID.match(cid):
        add(("id",), "检查 id 只能用小写字母、数字和下划线，以字母开头，最多 32 个字符")
    label = check.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 100:
        add(("label",), "检查的名称要写 1–100 个字符")
    kind = check.get("kind")
    if kind not in KINDS:
        add(("kind",), "检查的 kind 只能是 " + "、".join(KINDS))
        return found
    if check.get("severity", "error") not in SEVERITIES:
        add(("severity",), "severity 只能是 " + "、".join(SEVERITIES))
    why = check.get("why")
    if why is not None and (not isinstance(why, str) or len(why) > MAX_WHY):
        add(("why",), f"why 要是 {MAX_WHY} 个字符以内的文字")
    for key in check:
        if key not in COMMON | FIELDS[kind]:
            add((key,), f"{kind} 检查没有 {key} 这一项")

    if kind == "reconcile":
        for side in ("left", "right"):
            value = check.get(side)
            if not isinstance(value, dict):
                add((side,), f"{side} 要写成 {{table, expr, where}}")
                continue
            for key in value:
                if key not in SIDE:
                    add((side, key), f"{side} 没有 {key} 这一项")
            _table(value, "table", (side,), add)
            _expression(value, "expr", (side,), add, "聚合表达式", required=True)
            _expression(value, "where", (side,), add, "条件")
        _columns(check, "group_by", add, "分组列", MAX_GROUP_COLUMNS, reserved="value")
        _tolerance(check, add)
        if not isinstance(check.get("relative", False), bool):
            add(("relative",), "relative 要是 true 或 false")
        _known_problems(check, add)
        return found

    _table(check, "table", (), add)
    if kind == "aggregate":
        _expression(check, "expr", (), add, "聚合表达式", required=True)
        _expression(check, "where", (), add, "条件")
        _columns(check, "group_by", add, "分组列", MAX_GROUP_COLUMNS, reserved="value")
        _comparison(check, add)
    elif kind == "unique":
        _columns(check, "columns", add, "列", MAX_KEYS, required=True, reserved="count")
        _expression(check, "where", (), add, "条件")
    elif kind == "golden":
        if check.get("expected") is not None:
            _table(check, "expected", (), add)
        _columns(check, "keys", add, "keys ", MAX_KEYS, required=True)
        _columns(check, "columns", add, "columns ", MAX_COMPARED)
        _expression(check, "where", (), add, "条件")
        _expression(check, "expected_where", (), add, "expected_where ")
        _tolerance(check, add)
    else:
        _expression(check, "where", (), add, "条件", required=True)
        _lookup_problems(check, add)
    _known_problems(check, add)
    return found


def _lookup_problems(check: dict[str, Any], add: Callable[[Loc, str], None]) -> None:
    """``lookup`` on a rows check: only the rows that do (``found``) or do not have a row
    in another table with the same values in the ``on`` columns, among that table's rows
    its ``where`` keeps ("lanes with no allocation that applies this month")."""
    lookup = check.get("lookup")
    if lookup is None:
        return
    shape = '{"table", "on": {"本表的列": "另一张表的列"}, "where"?, "found": true/false}'
    if not isinstance(lookup, dict):
        add(("lookup",), f"lookup 要写成 {shape}")
        return
    for key in lookup:
        if key not in LOOKUP:
            add(("lookup", key), f"lookup 没有 {key} 这一项，只有 table、on、where、found、trim")
    _table(lookup, "table", ("lookup",), add)
    on = lookup.get("on")
    if (
        not isinstance(on, dict)
        or not on
        or len(on) > MAX_LOOKUP_COLUMNS
        or not all(
            isinstance(v, str) and _COLUMN.match(v) and v == v.strip()
            for pair in on.items()
            for v in pair
        )
    ):
        add(
            ("lookup", "on"),
            f"lookup.on 要写 1–{MAX_LOOKUP_COLUMNS} 对列名：{{本表的列: 另一张表的列}}，不带反引号",
        )
    elif len({k.lower() for k in on}) != len(on):
        add(("lookup", "on"), "lookup.on 里本表的列有重复")
    _expression(lookup, "where", ("lookup",), add, "lookup 的条件")
    if not isinstance(lookup.get("found"), bool):
        add(
            ("lookup", "found"),
            "lookup.found 要写 true（只看找得到对应行的行）或 false（只看找不到的行）",
        )
    if "trim" in lookup and not isinstance(lookup["trim"], bool):
        add(("lookup", "trim"), "lookup.trim 要写 true（两边去掉首尾空格再比）或 false")


def known_columns(check: dict[str, Any]) -> list[str] | None:
    """The columns a known difference of ``check`` is matched on: the groups of a grouped
    aggregate or reconcile, the columns of unique, the keys of golden; None for rows (any
    of the table's columns); [] when the check has no rows to tell apart."""
    kind = check.get("kind")
    key = {"aggregate": "group_by", "reconcile": "group_by", "unique": "columns", "golden": "keys"}
    if kind == "rows":
        return None
    found = check.get(key.get(str(kind), ""))
    return [c for c in found if isinstance(c, str)] if isinstance(found, list) else []


def _known_problems(check: dict[str, Any], add: Callable[[Loc, str], None]) -> None:
    """``known``: the differences people already know of (jadeflow acceptance I-4), each
    ``{"match": {column: value}, "note": "why"}``. What they match is reported apart and
    does not fail the check; anything else still does. A golden check's names the
    ``columns`` that may differ (with the values on each side, when given), a grouped
    aggregate's its ``value`` and a reconcile's its ``left`` and ``right``: anything else on
    that row or group still fails (jadeflow retest I-16)."""
    known = check.get("known")
    if known is None:
        return
    shape = '{"match": {"列": 值}, "note": "说明"}'
    if not isinstance(known, list) or not known:
        add(("known",), f"known（已知差异）要写成列表，每项是 {shape}")
        return
    if len(known) > MAX_KNOWN:
        add(("known",), f"known 最多 {MAX_KNOWN} 项")
    allowed = known_columns(check)
    if allowed == []:
        add(
            ("known",),
            "只有按组比的聚合值和对账、键不重复、和老系统对比、不该有的行能写 known（已知差异）",
        )
        return
    kind = check["kind"]
    what = {"unique": "检查的 columns", "golden": "keys"}.get(kind, "group_by")
    more = KNOWN_MORE.get(kind, ())
    fields = "、".join(["match", "note", *more])
    for i, item in enumerate(known):
        at: Loc = ("known", i)
        if not isinstance(item, dict):
            add(at, f"每项已知差异要写成 {shape}")
            continue
        for key in item:
            if key not in KNOWN_FIELDS and key not in more:
                add((*at, key), f"已知差异没有 {key} 这一项，只有 {fields}")
        match = item.get("match")
        if not isinstance(match, dict) or not match:
            add((*at, "match"), "match 要写成 {列: 值}，至少一列")
            continue
        for column, value in match.items():
            if allowed is not None and column not in allowed:
                add((*at, "match", column), f"列 {column} 要是{what}里的列：{'、'.join(allowed)}")
            elif allowed is None and not (_COLUMN.match(column) and column == column.strip()):
                add((*at, "match", column), "列名要是文字，不能有反引号")
            if not _plain_value(value):
                add((*at, "match", column), "值只能是文字、数字、true、false 或 null")
        note = item.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
            add((*at, "note"), f"note 要是 {MAX_NOTE} 个字符以内的文字")
        if kind == "golden" and "columns" in item:
            _known_columns_problems(check, item["columns"], (*at, "columns"), add)
        for key in ("value", "left", "right"):
            if key in more and key in item and not _plain_value(item[key]):
                add((*at, key), f"{key} 只能是文字、数字、true、false 或 null")


def _plain_value(value: Any) -> bool:
    return value is None or isinstance(value, str | bool) or _number(value)


def _known_columns_problems(
    check: dict[str, Any], columns: Any, at: Loc, add: Callable[[Loc, str], None]
) -> None:
    """A golden check's known difference names the columns that may differ: a list of
    them, or ``{column: {"table": value, "expected": value}}`` with the values seen."""
    shape = (
        'columns 要写成可能不一致的列名列表，或 {列: {"table": 结果表的值, "expected": 老系统的值}}'
    )
    if isinstance(columns, list):
        names, pins = columns, None
    elif isinstance(columns, dict):
        names, pins = list(columns), columns
    else:
        add(at, shape)
        return
    if not 1 <= len(names) <= MAX_KNOWN_COLUMNS:
        add(at, f"columns 要写 1–{MAX_KNOWN_COLUMNS} 列")
        return
    keys = {k.lower() for k in check.get("keys") or [] if isinstance(k, str)}
    listed = check.get("columns")
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not _COLUMN.match(name) or name != name.strip():
            add(at, "columns 里的列名要是文字，不能有反引号")
            continue
        if name.lower() in seen:
            add((*at, name), f"列 {name} 写了两次")
        seen.add(name.lower())
        if name.lower() in keys:
            add((*at, name), f"列 {name} 是对齐用的键，键不会不一致；已知差异的键写在 match 里")
        elif isinstance(listed, list) and name not in listed:
            add((*at, name), f"列 {name} 不在这项检查比较的 columns 里")
        if pins is None:
            continue
        pin = pins[name]
        if not isinstance(pin, dict) or not pin or not set(pin) <= KNOWN_SIDES:
            add((*at, name), shape)
            continue
        for side, value in pin.items():
            if not _plain_value(value):
                add((*at, name, side), "值只能是文字、数字、true、false 或 null")


def tables(check: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """The tables a check reads, with where in it: ``table``, golden's ``expected``,
    reconcile's ``left.table`` and ``right.table``."""
    for key in ("table", "expected"):
        if key in check:
            yield key, check[key]
    for side in ("left", "right", "lookup"):
        if isinstance(check.get(side), dict) and "table" in check[side]:
            yield f"{side}.table", check[side]["table"]


def texts(check: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """The SQL texts of a check (where ``${param.*}`` may stand), with where in it."""
    for key in ("expr", "where", "expected_where"):
        if key in check:
            yield key, check[key]
    for side in ("left", "right", "lookup"):
        if isinstance(check.get(side), dict):
            for key in ("expr", "where"):
                if key in check[side]:
                    yield f"{side}.{key}", check[side][key]


# Which columns a check names (jadeflow retest I-10: a model check said max(c), and the
# result table has no c). Read without parsing, so only what is surely a column counts.
_SQL_WORDS = frozenset(
    """
    and or not null true false is in like ilike rlike regexp between case when then else end
    as distinct interval cast try_cast exists all any some over partition by order asc desc
    nulls first last rows range unbounded preceding following current row filter within group
    escape from for to with where select limit both leading trailing position extract
    substring overlay placing int integer bigint smallint tinyint long string varchar char
    double float real decimal dec numeric date timestamp timestamp_ntz timestamp_ltz boolean
    binary array map struct void year years month months day days hour hours minute minutes
    second seconds week weeks quarter millisecond milliseconds microsecond microseconds
    current_date current_timestamp current_timezone current_user current_catalog
    current_schema current_database
    """.split()
)
_LITERAL = re.compile(r"(?<![\w])[xXrRbB]?(?:'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\")")
_QUOTED = re.compile(r"`((?:[^`]|``)+)`")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LAMBDA = re.compile(r"(?:\(([^()]*)\)|([A-Za-z_][A-Za-z0-9_]*))\s*->")


def columns_named(text: Any) -> list[str]:
    """The column names an expression or condition names, as far as can be told without
    parsing it: backquoted names, and bare words that are no keyword or type, function,
    qualifier or struct field, alias after AS, lambda variable or ``${param.*}``."""
    if not isinstance(text, str):
        return []
    found: list[str] = []
    rest = PARAM.sub(" ", _LITERAL.sub(" ", text))
    for quoted in _QUOTED.findall(rest):
        found.append(quoted.replace("``", "`"))
    rest = _QUOTED.sub(" __aep_quoted ", rest)
    bound = set()
    for listed, single in _LAMBDA.findall(rest):
        names = listed.split(",") if listed else [single]
        bound |= {n.strip().lower() for n in names if n.strip()}
    previous = ""
    for m in _WORD.finditer(rest):
        word = m.group()
        before = rest[: m.start()].rstrip()
        after = rest[m.end() :].lstrip()
        start_ok = m.start() == 0 or not (
            rest[m.start() - 1].isalnum() or rest[m.start() - 1] in "_$."
        )
        skip = (
            not start_ok
            or word == "__aep_quoted"
            or word.lower() in _SQL_WORDS
            or word.lower() in bound
            or after.startswith(("(", ".", "->"))
            or before.endswith(".")
            or previous.lower() == "as"
        )
        previous = word
        if not skip:
            found.append(word)
    return list(dict.fromkeys(found))


def column_uses(check: dict[str, Any]) -> Iterator[tuple[str, str, list[str]]]:
    """Where a check names columns: the table they belong to, where in the check, and the
    names (``group_by`` names columns of both sides of a reconcile; a lookup's ``on``
    pairs a column of the check's table with one of the other table)."""
    kind = check.get("kind")

    def names(value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    def side(value: Any, where: str) -> Iterator[tuple[str, str, list[str]]]:
        if not isinstance(value, dict) or not isinstance(value.get("table"), str):
            return
        for key in ("expr", "where"):
            if found := columns_named(value.get(key)):
                yield value["table"], f"{where}{key}", found

    if kind == "reconcile":
        for name in ("left", "right"):
            yield from side(check.get(name), f"{name}.")
            if isinstance(check.get(name), dict) and (grouped := names(check.get("group_by"))):
                yield str(check[name].get("table")), "group_by", grouped
        return
    if kind not in ("aggregate", "unique", "rows"):
        return
    yield from side(check, "")
    table = check.get("table")
    if not isinstance(table, str):
        return
    if listed := names(check.get("group_by") if kind == "aggregate" else check.get("columns")):
        yield table, "group_by" if kind == "aggregate" else "columns", listed
    lookup = check.get("lookup") if kind == "rows" else None
    if isinstance(lookup, dict) and isinstance(lookup.get("on"), dict):
        on = lookup["on"]
        yield table, "lookup.on", [k for k in on if isinstance(k, str)]
        if isinstance(lookup.get("table"), str):
            yield lookup["table"], "lookup.on", [v for v in on.values() if isinstance(v, str)]
            if found := columns_named(lookup.get("where")):
                yield lookup["table"], "lookup.where", found


def _table(value: dict[str, Any], key: str, loc: Loc, add: Callable[[Loc, str], None]) -> None:
    name = value.get(key)
    if not isinstance(name, str) or not TABLE.match(name):
        add(
            (*loc, key),
            "表名要写成结果表的名字、schema.table（抽取的源表）或 catalog.schema.table，不带反引号",
        )


def _expression(
    value: dict[str, Any],
    key: str,
    loc: Loc,
    add: Callable[[Loc, str], None],
    label: str,
    required: bool = False,
) -> None:
    text = value.get(key)
    if text is None:
        if required:
            add((*loc, key), f"{label}不能为空")
        return
    if not isinstance(text, str) or not text.strip():
        add((*loc, key), f"{label}要写成不为空的文字")
        return
    if len(text) > MAX_EXPRESSION:
        add((*loc, key), f"{label}最多 {MAX_EXPRESSION} 个字符")
        return
    problem = filter_problem(text)
    if problem is not None and problem.endswith("SELECT"):
        # Say what to write instead (jadeflow acceptance S-11): a check reads one table
        # through the read-only connector, so comparing with another table is a reconcile.
        add(
            (*loc, key),
            f"{label}不能有子查询（SELECT）：要和另一张表比，用 reconcile 对账检查，两边各写"
            "表、聚合表达式和条件，按组比时写 group_by；要找出不该有的行，用 rows 检查，"
            "条件只写这张表的列；要找出在另一张表里找不到（或找得到）对应行的行，用 rows 检查"
            '加 lookup：{"table": 另一张表, "on": {本表的列: 另一张表的列}, "where": 另一张表'
            '的条件, "found": false}',
        )
    elif problem is not None:
        add((*loc, key), problem.replace("读取筛选", label))
    elif "${" in PARAM.sub("", text):
        add((*loc, key), "运行参数要写成 ${param.<key>}（key 是项目参数的标识）")


def _columns(
    check: dict[str, Any],
    key: str,
    add: Callable[[Loc, str], None],
    label: str,
    most: int,
    required: bool = False,
    reserved: str | None = None,
) -> None:
    value = check.get(key)
    if value is None and not required:
        return
    if not isinstance(value, list) or not value or len(value) > most:
        add((key,), f"{label}要写 1–{most} 个列名")
        return
    if not all(isinstance(v, str) and _COLUMN.match(v) and v == v.strip() for v in value):
        add((key,), f"{label}的列名要是文字，不能有反引号")
        return
    lowered = [v.lower() for v in value]
    if len(set(lowered)) != len(lowered):
        add((key,), f"{label}有重复的列")
    elif reserved is not None and reserved in lowered:
        add((key,), f"{label}不能叫 {reserved}（和结果里的 {reserved} 重名）")


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _tolerance(check: dict[str, Any], add: Callable[[Loc, str], None]) -> None:
    value = check.get("tolerance", 0)
    if not _number(value) or value < 0:
        add(("tolerance",), "tolerance 要是不小于 0 的数")


def _comparison(check: dict[str, Any], add: Callable[[Loc, str], None]) -> None:
    op = check.get("op")
    if op not in OPS:
        add(("op",), "op 只能是 " + "、".join(OPS))
        return
    value, top = check.get("value"), check.get("max")
    if op == "between":
        if not _number(value) or not _number(top):
            add(("value",), "between 要写数字 value 和 max")
        elif value > top:
            add(("max",), "max 不能比 value 小")
    else:
        if top is not None:
            add(("max",), "只有 between 要写 max")
        allowed = (int, float, str, bool) if op in ("eq", "ne") else (int, float, str)
        if (
            value is None
            or not isinstance(value, allowed)
            or (isinstance(value, bool) and bool not in allowed)
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            kinds = "数字、文字或 true/false" if op in ("eq", "ne") else "数字或文字"
            add(("value",), f"value 要是{kinds}")
    _tolerance(check, add)


# Which tables and rows a check reads


class CheckError(Exception):
    """Why one check could not be worked out; it is recorded on that check."""


@dataclass(frozen=True)
class Scope:
    """Where a run's tables are. ``results`` is the catalog.schema of its result tables
    (bare names; None with ``no_results`` saying why), ``sources`` the catalog of the
    tables it ingested (two-part names), ``filters`` the read filters of the project's
    references by lowercased three-part name, ``params`` the run's parameter values (None
    when no project is bound) and ``kinds`` their types."""

    results: str | None
    no_results: str
    sources: str | None
    filters: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None
    kinds: dict[str, str] = field(default_factory=dict)

    def table(self, name: str) -> str:
        """``name`` as catalog.schema.table."""
        parts = name.count(".")
        if parts == 0:
            if self.results is None:
                raise CheckError(self.no_results)
            return f"{self.results}.{name}"
        if parts == 1:
            if not self.sources:
                raise CheckError(
                    f"平台没有配置 AEP_DATABRICKS_SOURCE_CATALOG，读不到 {name}；"
                    "请写成 catalog.schema.table"
                )
            return f"{self.sources}.{name}"
        return name

    def text(self, text: str) -> str:
        """``text`` with its run parameters as SQL literals."""
        if self.params is None:
            found = PARAM.search(text)
            if found:
                raise CheckError(f"这次运行没有绑定项目，不能用运行参数 {found[0]}")
            return text
        try:
            return with_params(text, self.params, self.kinds)
        except ValueError as exc:
            raise CheckError(str(exc)) from exc

    def condition(self, table: str, where: str | None) -> str | None:
        """The rows of ``table`` (three-part) a check reads: the project's read filter on
        it and the check's own condition for this side, joined with AND."""
        found = [
            text
            for text in (self.filters.get(table.lower()), self.text(where) if where else None)
            if text
        ]
        if len(found) == 2:
            return f"({found[0]}) AND ({found[1]})"
        return found[0] if found else None


Call = Callable[[str, dict[str, Any]], Awaitable[Any]]


# Running a check


def result(check: dict[str, Any], status: str, message: str, **found: Any) -> dict[str, Any]:
    rows = found.get("rows") or []
    focus = focus_columns(check, rows) if rows else []
    return (
        {
            "id": check.get("id"),
            "label": check.get("label"),
            "kind": check.get("kind"),
            "severity": check.get("severity", "error"),
            "status": status,
            "actual": found.get("actual"),
            "expected": found.get("expected"),
            "message": message,
            "rows": rows,
        }
        | ({"known": found["known"]} if found.get("known") is not None else {})
        | ({"focus": focus} if focus else {})
    )


def focus_columns(check: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """The columns an example row is read by, shown before the others (jadeflow retest
    S-15: the differing month was cut off after the first ten columns): a golden
    difference's kind, key and differing columns; a rows check's lookup key and the
    columns its condition names. Only columns the example rows have, as they spell them."""
    kind = check.get("kind")
    if kind == "golden":
        keys = [k for r in rows if isinstance(r.get("key"), dict) for k in r["key"]]
        differs = {str(c) for r in rows for c in r.get("differs") or []}
        compared = [c for c in check.get("columns") or [] if c in differs]
        named = [*compared, *sorted(differs - set(compared))]
        return list(dict.fromkeys(["差异", *keys, *named])) if keys else []
    if kind != "rows":
        return []
    present = {str(c).lower(): str(c) for r in rows for c in r}
    lookup = check.get("lookup") if isinstance(check.get("lookup"), dict) else {}
    on = lookup.get("on") if isinstance(lookup.get("on"), dict) else {}
    named = [*on, *columns_named(check.get("where"))]
    return list(dict.fromkeys(present[n.lower()] for n in named if n.lower() in present))


async def run(
    check: dict[str, Any], scope: Scope, call: Call, expected: str | None = None
) -> dict[str, Any]:
    """The result of one valid check; ``expected`` is the old system's table a golden
    check without one of its own compares with. A check that fails to run is an error
    on its own; a connector that cannot be reached is not (``call`` raises)."""
    try:
        return await RUNS[check["kind"]](check, scope, call, expected)
    except CheckError as exc:
        return result(check, "error", str(exc))
    except ConnectorUnavailable:
        raise
    except ToolError as exc:
        return result(check, "error", _first_line(TOOL_PREFIX.sub("", str(exc))))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return result(check, "error", f"连接器返回的结果看不懂：{_first_line(str(exc))}")


def _first_line(text: str) -> str:
    return (text.strip().splitlines() or ["（没有报错内容）"])[0][:500]


def _args(table: str, where: str | None, **more: Any) -> dict[str, Any]:
    return {"table": table, **more} | ({"where": where} if where else {})


def _all_groups(found: dict[str, Any]) -> list[dict[str, Any]]:
    rows, groups = found["rows"], int(found["groups"])
    if groups > len(rows):
        raise CheckError(
            f"分组有 {groups:,} 组，超过一次能核对的 {GROUP_LIMIT:,} 组；请减少分组列或加条件"
        )
    return list(rows)


def _meets(actual: Any, check: dict[str, Any]) -> bool:
    """Whether an aggregate's value meets the check's ``op``; a null never does."""
    if actual is None:
        return False
    op, value, tolerance = check["op"], check.get("value"), check.get("tolerance", 0)
    numeric = op == "between" or _number(value)
    if numeric:
        if not _number(actual):
            raise CheckError(f"聚合结果 {fmt(actual)} 不是数字，不能和 {condition(check)} 比较")
        if op == "between":
            return value - tolerance <= actual <= check["max"] + tolerance
        if op == "eq":
            return abs(actual - value) <= tolerance
        if op == "ne":
            return abs(actual - value) > tolerance
    elif op == "eq":
        return actual == value
    elif op == "ne":
        return actual != value
    elif not isinstance(actual, str):
        raise CheckError(f"聚合结果 {fmt(actual)} 不是文字，不能和 {condition(check)} 比较")
    return {
        "gt": actual > value,
        "ge": actual >= value,
        "lt": actual < value,
        "le": actual <= value,
    }[op]


def condition(check: dict[str, Any]) -> str:
    """The check's ``op`` as people read it, like ``≥ 0`` or ``在 1 和 10 之间``."""
    op, tolerance = check["op"], check.get("tolerance", 0)
    if op == "between":
        text = f"在 {fmt(check['value'])} 和 {fmt(check['max'])} 之间"
    else:
        text = f"{SIGN[op]} {fmt(check['value'])}"
    if tolerance and op in ("eq", "ne", "between"):
        text += f"（容差 {fmt(tolerance)}）"
    return text


KIND_TEXT = {
    "aggregate": "聚合值",
    "reconcile": "对账",
    "unique": "键不重复",
    "golden": "和老系统对比",
    "rows": "不该有的行",
}


def describe(check: dict[str, Any]) -> str:
    """What a valid check checks, in one sentence of Markdown for a person (a plan shows
    the checks it proposes this way)."""
    said = _describe(check)
    known = check.get("known") or []
    if known:
        said += f"；已知差异 {len(known)} 处（{'；'.join(_named(k) for k in known)}）不算不通过"
    return said


def _describe(check: dict[str, Any]) -> str:
    kind = check["kind"]

    def code(text: str) -> str:
        return f"`{text}`"

    def under(where: Any) -> str:
        return f"（条件 {code(where)}）" if where else ""

    def by() -> str:
        columns = check.get("group_by") or []
        return f"按 {'、'.join(map(code, columns))} 分组，" if columns else ""

    if kind == "aggregate":
        each = "每组" if check.get("group_by") else ""
        return (
            f"{code(check['table'])} {by()}{each}{code(check['expr'])} {condition(check)}"
            + under(check.get("where"))
        )
    if kind == "reconcile":
        left, right = check["left"], check["right"]
        tolerance = check.get("tolerance", 0)
        allowed = ""
        if tolerance:
            shown = f"{fmt(tolerance * 100)}%" if check.get("relative") else fmt(tolerance)
            allowed = f"（容差 {shown}）"
        return (
            f"{by()}{code(left['table'])} 的 {code(left['expr'])}{under(left.get('where'))}"
            f"和 {code(right['table'])} 的 {code(right['expr'])}{under(right.get('where'))}"
            f"一致{allowed}"
        )
    if kind == "unique":
        columns = "、".join(map(code, check["columns"]))
        return f"{code(check['table'])} 按 {columns} 没有重复的行" + under(check.get("where"))
    if kind == "golden":
        keys = "、".join(map(code, check.get("keys") or []))
        expected = check.get("expected")
        old = f"老系统的 {code(expected)}" if expected else "老系统的结果表"
        return f"{code(check['table'])} 和{old}按 {keys} 逐行一致"
    return f"{code(check['table'])} 里没有{_rows_said(check, code)}"


def _rows_said(check: dict[str, Any], code: Callable[[str], str] = str) -> str:
    """What rows a rows check looks for: ``满足 … 的行``, with a lookup ``满足 …、并且在 …
    里找不到 … 对应（满足 …）的行``."""
    said = f"满足 {code(check['where'])}"
    lookup = check.get("lookup")
    if not lookup:
        return said + " 的行"
    on = "、".join(a if a == b else f"{a} = {b}" for a, b in lookup["on"].items())
    where = f"（满足 {code(lookup['where'])}）" if lookup.get("where") else ""
    found = "找得到" if lookup["found"] else "找不到"
    trimmed = "（去掉首尾空格后）" if lookup.get("trim") else ""
    return f"{said}、并且在 {code(lookup['table'])} 里{found} {on}{trimmed} 对应{where}的行"


async def _aggregate(
    check: dict[str, Any], scope: Scope, call: Call, expected: str | None
) -> dict[str, Any]:
    table = scope.table(check["table"])
    args = _args(table, scope.condition(table, check.get("where")), expr=scope.text(check["expr"]))
    group_by = check.get("group_by")
    if not group_by:
        found = await call("aggregate", args)
        actual = found["rows"][0]["value"] if found["rows"] else None
        ok = _meets(actual, check)
        wanted = [check["value"], check["max"]] if check["op"] == "between" else check["value"]
        message = f"实际 {fmt(actual)}，要求 {condition(check)}，{'满足' if ok else '不满足'}"
        return result(check, _status(ok), message, actual=actual, expected=wanted)
    rows = _all_groups(await call("aggregate", args | {"group_by": group_by, "limit": GROUP_LIMIT}))
    bad, known = _apart([row for row in rows if not _meets(row.get("value"), check)], check)
    named = "、".join(group_by)
    if not rows:
        message = f"按 {named} 分组没有行"
    elif bad:
        message = f"按 {named} 分 {len(rows):,} 组，{len(bad):,} 组不满足 {condition(check)}"
    else:
        but = "除已知差异外" if known and known["count"] else ""
        message = f"按 {named} 分 {len(rows):,} 组，{but}每组都满足 {condition(check)}"
    return result(
        check,
        _status(not bad),
        message + _known_tail(known, "组"),
        actual=len(bad),
        expected=0,
        rows=bad[:EXAMPLES],
        known=known,
    )


def _same(left: Any, right: Any, check: dict[str, Any]) -> tuple[bool, str]:
    """Whether two sides agree within the check's tolerance, and how far apart they are
    as people read it ("" when they are not two numbers)."""
    tolerance, relative = check.get("tolerance", 0), check.get("relative", False)
    if not (_number(left) and _number(right)):
        return left == right, ""
    gap = abs(left - right)
    if relative:
        gap = gap / abs(right) if right else (0.0 if gap == 0 else math.inf)
        shown = "无穷大" if math.isinf(gap) else f"{fmt(gap * 100)}%"
        allowed = f"{fmt(tolerance * 100)}%"
    else:
        shown, allowed = fmt(gap), fmt(tolerance)
    if gap == 0:
        return True, ""
    if gap <= tolerance:
        return True, f"相差 {shown}，在容差 {allowed} 内"
    return False, f"相差 {shown}" + (f"，超过容差 {allowed}" if tolerance else "")


def _key(row: dict[str, Any], columns: list[str]) -> str:
    """A group's key; 2026 and 2026.0 (an integer and a decimal column) are one group."""
    values = [row.get(c) for c in columns]
    values = [int(v) if isinstance(v, float) and v.is_integer() else v for v in values]
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


# Known differences (jadeflow acceptance I-4): what they match is reported apart.


def _plain(value: Any) -> Any:
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _equal(found: Any, wanted: Any) -> bool:
    """Whether a row's value is a known difference's (2026 and "2026" are one value)."""
    a, b = _plain(found), _plain(wanted)
    if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
        return a is b if a is None or b is None else a == b and type(a) is type(b)
    return a == b or str(a) == str(b)


def _which(row: Any, known: list[dict[str, Any]]) -> int | None:
    """The known difference ``row`` is, if any."""
    if not isinstance(row, dict):
        return None
    for i, item in enumerate(known):
        if all(_equal(row.get(c), v) for c, v in item["match"].items()):
            return i
    return None


def _named(item: dict[str, Any]) -> str:
    """A known difference as people read it: its note, or the values it matches (written as
    given, so a year stays 2026, not 2,026)."""

    def value(v: Any) -> str:
        if v is None:
            return "空值"
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(_plain(v))

    if item.get("note"):
        return str(item["note"])
    said = "、".join(f"{c} = {value(v)}" for c, v in item["match"].items())
    columns = item.get("columns")
    return f"{said} 的 {'、'.join(columns)}" if columns else said


def _known_seen(
    known: list[dict[str, Any]],
    count: int,
    hit: set[int],
    complete: bool,
    beyond: set[int] | None = None,
) -> dict[str, Any]:
    """``count`` rows or groups matched known differences, ``hit`` the ones seen among them;
    with ``complete`` (all of them were looked at) the ones that did not show up too.
    ``beyond``: the known differences whose row or group differed in more than they say,
    which failed as usual (jadeflow retest I-16)."""
    beyond = beyond or set()
    return {
        "count": count,
        "seen": [_named(known[i]) for i in sorted(hit)],
        "absent": [_named(k) for i, k in enumerate(known) if i not in hit and i not in beyond]
        if complete
        else [],
        "beyond": [_named(known[i]) for i in sorted(beyond)],
    }


def _close(found: Any, wanted: Any, check: dict[str, Any] | None = None) -> bool:
    """Whether a value is the one a known difference pins: numbers (also written as text,
    as a decimal column comes back) within the check's tolerance, else the same value."""
    a, b = _as_number(found), _as_number(wanted)
    if a is None or b is None:
        return _equal(found, wanted)
    check = check or {}
    tolerance = check.get("tolerance", 0) or 0
    allowed = tolerance * abs(b) if check.get("relative") else tolerance
    return abs(a - b) <= max(allowed, 1e-9 * max(1.0, abs(b)))


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if _number(value):
        return float(value)
    if isinstance(value, str):
        try:
            found = float(value)
        except ValueError:
            return None
        return found if math.isfinite(found) else None
    return None


def _pinned(row: dict[str, Any], item: dict[str, Any], check: dict[str, Any]) -> bool:
    """Whether a failing group is the one a known difference says: its ``value`` (grouped
    aggregate) or ``left`` and ``right`` (reconcile), where given."""
    return all(
        _close(row.get(key), item[key], check)
        for key in KNOWN_MORE.get(check["kind"], ())
        if key in item and key != "columns"
    )


def _apart(
    rows: list[dict[str, Any]], check: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The failing groups that are no known difference, and what the known ones were."""
    known = check.get("known")
    if not known:
        return rows, None
    new: list[dict[str, Any]] = []
    hit: set[int] = set()
    beyond: set[int] = set()
    count = 0
    for row in rows:
        i = _which(row, known)
        if i is None:
            new.append(row)
        elif not _pinned(row, known[i], check):
            beyond.add(i)
            new.append(row)
        else:
            hit.add(i)
            count += 1
    return new, _known_seen(known, count, hit, complete=True, beyond=beyond)


def _known_tail(info: dict[str, Any] | None, unit: str) -> str:
    if info is None:
        return ""
    parts = []
    if info["count"]:
        parts.append(f"另有 {info['count']:,} {unit}是已知差异（{'；'.join(info['seen'])}）")
    if info.get("beyond"):
        parts.append(
            f"已知差异「{'；'.join(info['beyond'])}」的那{unit}和清单写的不一样或还有别的不一致，"
            "照常算不通过"
        )
    if info["absent"]:
        parts.append(f"已知差异「{'；'.join(info['absent'])}」这次没有出现")
    return "".join(f"；{p}" for p in parts)


def _sql_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _known_sql(known: list[dict[str, Any]]) -> str:
    """The rows the known differences match, as a condition (null matches null)."""
    return " OR ".join(
        "("
        + " AND ".join(
            f"`{c.replace('`', '``')}` <=> {_sql_value(v)}" for c, v in item["match"].items()
        )
        + ")"
        for item in known
    )


def _and(where: str | None, more: str) -> str:
    found = f"({where}) AND ({more})" if where else more
    if len(found) > MAX_EXPRESSION:
        raise CheckError(
            f"加上已知差异后条件超过 {MAX_EXPRESSION} 个字符，连接器不收；请少写几项已知差异"
        )
    return found


async def _reconcile(
    check: dict[str, Any], scope: Scope, call: Call, expected: str | None
) -> dict[str, Any]:
    sides = []
    for side in (check["left"], check["right"]):
        table = scope.table(side["table"])
        where = scope.condition(table, side.get("where"))
        sides.append(_args(table, where, expr=scope.text(side["expr"])))
    group_by = check.get("group_by")
    if not group_by:
        left_found = await call("aggregate", sides[0])
        right_found = await call("aggregate", sides[1])
        left = left_found["rows"][0]["value"] if left_found["rows"] else None
        right = right_found["rows"][0]["value"] if right_found["rows"] else None
        same, gap = _same(left, right, check)
        if same:
            tail = gap or "一致"
        elif gap and not check.get("tolerance"):
            tail = f"{gap}，不一致"
        else:
            tail = gap or "不一致"
        message = f"左边 {fmt(left)}，右边 {fmt(right)}，{tail}"
        return result(check, _status(same), message, actual=left, expected=right)

    more = {"group_by": group_by, "limit": GROUP_LIMIT}
    left_rows = _all_groups(await call("aggregate", sides[0] | more))
    right_rows = _all_groups(await call("aggregate", sides[1] | more))
    lefts = {_key(r, group_by): r for r in left_rows}
    rights = {_key(r, group_by): r for r in right_rows}
    bad = []
    for key in dict.fromkeys([*lefts, *rights]):
        a, b = lefts.get(key), rights.get(key)
        row = {c: (a or b or {}).get(c) for c in group_by}
        if a is None or b is None or not _same(a.get("value"), b.get("value"), check)[0]:
            bad.append(row | {"left": (a or {}).get("value"), "right": (b or {}).get("value")})
    total = len(set(lefts) | set(rights))
    bad, known = _apart(bad, check)
    named = "、".join(group_by)
    if not total:
        message = f"按 {named} 分组两边都没有行"
    elif bad:
        message = f"按 {named} 分 {total:,} 组，{len(bad):,} 组对不上"
    else:
        but = "除已知差异外" if known and known["count"] else ""
        message = f"按 {named} 分 {total:,} 组，{but}每组都一致"
    return result(
        check,
        _status(not bad),
        message + _known_tail(known, "组"),
        actual=len(bad),
        expected=0,
        rows=bad[:EXAMPLES],
        known=known,
    )


async def _unique(
    check: dict[str, Any], scope: Scope, call: Call, expected: str | None
) -> dict[str, Any]:
    table = scope.table(check["table"])
    columns = check["columns"]
    where = scope.condition(table, check.get("where"))
    found = await call("duplicates", _args(table, where, columns=columns, limit=EXAMPLES))
    groups, rows = int(found["groups"]), found["rows"]
    known: dict[str, Any] | None = None
    if check.get("known"):
        entries = check["known"]
        known = _known_seen(entries, 0, set(), complete=True)
        if groups:
            sql = _known_sql(entries)
            new = await call(
                "duplicates",
                _args(table, _and(where, f"NOT ({sql})"), columns=columns, limit=EXAMPLES),
            )
            seen = await call(
                "duplicates", _args(table, _and(where, sql), columns=columns, limit=EXAMPLES)
            )
            count = int(seen["groups"])
            hit = {i for r in seen["rows"] if (i := _which(r, entries)) is not None}
            known = _known_seen(entries, count, hit, complete=count <= len(seen["rows"]))
            groups, rows = int(new["groups"]), new["rows"]
    named = "、".join(columns)
    if groups:
        message = f"按 {named} 有 {groups:,} 组重复"
    elif known and known["count"]:
        message = f"按 {named} 除已知差异外没有重复"
    else:
        message = f"按 {named} 没有重复"
    return result(
        check,
        _status(not groups),
        message + _known_tail(known, "组"),
        actual=groups,
        expected=0,
        rows=rows,
        known=known,
    )


def _golden_known(
    found: dict[str, Any], seen: dict[str, Any], entries: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The comparison of the rows no known difference matches (``found``) with what the
    ones they match (``seen``) add: a row is known only when it differs in nothing but what
    its entry says; any other row of theirs counts as missing, extra or different."""
    repeated = seen["duplicates"]
    if int(repeated["table"]) or int(repeated["expected"]):
        both = found["duplicates"]
        merged = {
            "duplicates": {
                side: int(both[side]) + int(repeated[side]) for side in ("table", "expected")
            },
            "missing": None,
            "extra": None,
            "different": None,
            "columns": found.get("columns") or seen.get("columns"),
            "rows": [*seen["rows"], *(r for r in found["rows"] if r.get("kind") == "duplicate")],
        }
        return merged, _known_seen(entries, 0, set(), complete=False)
    total = sum(int(seen[k]) for k in ("missing", "extra", "different"))
    if total > len(seen["rows"]):
        raise CheckError(
            f"已知差异匹配到 {total:,} 行，超过一次能逐行核对的 {KNOWN_ROWS} 行；"
            "请把 match 写得更具体"
        )
    hit: set[int] = set()
    beyond: set[int] = set()
    real: list[dict[str, Any]] = []
    for row in seen["rows"]:
        i = _which(row.get("key"), entries)
        if i is not None and _golden_known_row(row, entries[i]):
            hit.add(i)
        else:
            if i is not None:
                beyond.add(i)
            real.append(row)
    known = _known_seen(entries, total - len(real), hit, complete=True, beyond=beyond)
    if not real or found["missing"] is None:
        return found, known
    merged = dict(found)
    for row in real:
        merged[row["kind"]] = int(merged[row["kind"]]) + 1
    merged["rows"] = [*real, *found["rows"]][:EXAMPLES]
    return merged, known


def _golden_known_row(row: dict[str, Any], item: dict[str, Any]) -> bool:
    """Whether a row of the comparison is what a golden check's known difference says:
    without ``columns`` a row on one side only; with them a row on both sides that differs
    in those columns alone, with the values given (I-16)."""
    columns = item.get("columns")
    kind = row.get("kind")
    if kind in ("missing", "extra"):
        return columns is None
    if kind != "different" or not columns:
        return False
    names = {str(c).lower() for c in columns}
    if not {str(c).lower() for c in row.get("differs") or []} <= names:
        return False
    if isinstance(columns, dict):
        for name, pin in columns.items():
            for side in ("table", "expected"):
                if side in pin and not _close(_cell_of(row.get(side), name), pin[side]):
                    return False
    return True


def _cell_of(values: Any, name: str) -> Any:
    """A column's value in one side of a compared row, by name in any case."""
    if not isinstance(values, dict):
        return None
    if name in values:
        return values[name]
    return next((v for k, v in values.items() if str(k).lower() == name.lower()), None)


async def _golden(
    check: dict[str, Any], scope: Scope, call: Call, expected: str | None
) -> dict[str, Any]:
    old_name = check.get("expected") or expected
    if not old_name:
        return result(check, "skipped", "没有指定老系统的结果表")
    keys = check["keys"]
    table, old = scope.table(check["table"]), scope.table(old_name)
    where = scope.condition(table, check.get("where"))
    expected_where = scope.condition(old, check.get("expected_where"))
    more: dict[str, Any] = {"expected": old, "keys": keys, "limit": EXAMPLES}
    if check.get("columns"):
        more["columns"] = check["columns"]
    if check.get("tolerance"):
        more["tolerance"] = check["tolerance"]

    def compare(
        table_where: str | None, old_where: str | None, limit: int = EXAMPLES
    ) -> Awaitable[Any]:
        args = _args(table, table_where, **(more | {"limit": limit}))
        if old_where:
            args["expected_where"] = old_where
        return call("compare", args)

    known: dict[str, Any] | None = None
    if check.get("known"):
        # The keys people know differ are compared apart from the rest, on both sides,
        # and looked at row by row: only what an entry says may differ (I-16).
        entries = check["known"]
        sql = _known_sql(entries)
        found = await compare(_and(where, f"NOT ({sql})"), _and(expected_where, f"NOT ({sql})"))
        seen = await compare(_and(where, sql), _and(expected_where, sql), KNOWN_ROWS)
        found, known = _golden_known(found, seen, entries)
    else:
        found = await compare(where, expected_where)
    tail = _known_tail(known, "行")
    named = "、".join(keys)
    repeated = found["duplicates"]
    counts = {
        "missing": found["missing"],
        "extra": found["extra"],
        "different": found["different"],
        "duplicates": {"table": int(repeated["table"]), "expected": int(repeated["expected"])},
    }
    rows = found["rows"]
    if counts["duplicates"]["table"]:
        n = counts["duplicates"]["table"]
        mine = [r for r in rows if r.get("side") == "table"]
        message = f"结果表按 {named} 有 {n:,} 组重复的行，不能逐行对比"
        return result(check, "failed", message, actual=counts, expected=old, rows=mine)
    if counts["duplicates"]["expected"]:
        message = f"老系统的结果表按 {named} 有重复，不能逐行对比；请换一组键"
        return result(check, "error", message, actual=counts, expected=old, rows=rows)
    missing, extra, different = (int(counts[k]) for k in ("missing", "extra", "different"))
    if missing or extra or different:
        message = (
            f"和老系统的结果对不上：少 {missing:,} 行、多 {extra:,} 行、{different:,} 行不一致"
        )
        return result(
            check, "failed", message + tail, actual=counts, expected=old, rows=rows, known=known
        )
    compared = found.get("columns") or []
    what = f"比较了 {'、'.join(compared)}" if compared else "两边没有同名的其他列，只比了键"
    but = "除已知差异外" if known and known["count"] else ""
    message = f"和老系统的结果{but}逐行一致（按 {named} 对齐，{what}）"
    return result(check, "passed", message + tail, actual=counts, expected=old, known=known)


async def _rows(
    check: dict[str, Any], scope: Scope, call: Call, expected: str | None
) -> dict[str, Any]:
    table = scope.table(check["table"])
    where = scope.condition(table, check["where"])
    schema, _, name = table.rpartition(".")
    more: dict[str, Any] = {}
    if lookup := check.get("lookup"):
        other = scope.table(lookup["table"])
        looked = {"table": other, "on": lookup["on"], "found": lookup["found"]}
        if lookup.get("trim"):
            looked["trim"] = True
        if condition := scope.condition(other, lookup.get("where")):
            looked["where"] = condition
        more = {"lookup": looked}

    async def counted(condition: str | None) -> int:
        found = await call("aggregate", _args(table, condition, expr="count(*)", **more))
        return int(found["rows"][0]["value"] or 0) if found["rows"] else 0

    async def sampled(condition: str | None) -> list[dict[str, Any]]:
        args = {"schema": schema, "table": name, "limit": EXAMPLES, "where": condition}
        return list((await call("sample_rows", args | more))["rows"])

    count = await counted(where)
    entries = check.get("known")
    known = _known_seen(entries, 0, set(), complete=True) if entries else None
    if count and entries:
        # The rows people know of are counted and shown apart (jadeflow acceptance I-4).
        sql = _known_sql(entries)
        new_where = _and(where, f"NOT ({sql})")
        new = await counted(new_where)
        seen = await sampled(_and(where, sql)) if new < count else []
        hit = {i for r in seen if (i := _which(r, entries)) is not None}
        known = _known_seen(entries, count - new, hit, complete=count - new <= len(seen))
        where, count = new_where, new
    tail = _known_tail(known, "行")
    said = _rows_said(check)
    if not count:
        but = "已知差异以外" if known and known["count"] else ""
        message = f"{but}没有{said}"
        return result(check, "passed", message + tail, actual=0, expected=0, known=known)
    message = f"有 {count:,} 行{said.removesuffix('的行').rstrip()}"
    return result(
        check,
        "failed",
        message + tail,
        actual=count,
        expected=0,
        rows=await sampled(where),
        known=known,
    )


def _status(ok: bool) -> str:
    return "passed" if ok else "failed"


RUNS: dict[str, Callable[[dict[str, Any], Scope, Call, str | None], Awaitable[dict[str, Any]]]]
RUNS = {
    "aggregate": _aggregate,
    "reconcile": _reconcile,
    "unique": _unique,
    "golden": _golden,
    "rows": _rows,
}


# The node's added comparison with the old system


def golden_check(
    table: Any, expected: Any, keys: Any, tolerance: Any = None, severity: Any = None
) -> tuple[dict[str, Any], str | None]:
    """The check a node adds when it is given the old system's table, and why it cannot
    run (None when it can). ``tolerance`` and ``severity`` are left out when not given."""
    check = {
        "id": GOLDEN,
        "label": GOLDEN_LABEL,
        "kind": "golden",
        "table": table,
        "expected": expected,
        "keys": keys,
    }
    if tolerance is not None:
        check["tolerance"] = tolerance
    if severity is not None:
        check["severity"] = severity
    if not isinstance(expected, str):
        return check, "老系统结果表的名字要写成文字（catalog.schema.table）"
    if not table:
        return check, "没有结果表，不能逐行对比"
    if not keys:
        return check, "结果表没有主键，不能逐行对比；请在方案里给结果表写 primary_key"
    found = problems(check)
    return check, found[0][1] if found else None


# Conclusions


def passed(results: list[dict[str, Any]]) -> bool:
    """No check of severity error failed or errored."""
    return not any(r["severity"] == "error" and r["status"] in ("failed", "error") for r in results)


def _soft(results: list[dict[str, Any]]) -> str:
    """The severities of these (failed or errored) checks, when not all are errors; the
    errors come first, so the count that keeps a result from passing is never left out."""
    counts = Counter(r["severity"] for r in results)
    if set(counts) == {"error"}:
        return ""
    if len(counts) == 1:
        return f"（{SEVERITY_TEXT[next(iter(counts))]}）"
    return (
        "（"
        + "，".join(f"{counts[s]} 项是{SEVERITY_TEXT[s]}" for s in SEVERITY_TEXT if counts[s])
        + "）"
    )


def summary(results: list[dict[str, Any]]) -> str:
    """Like "8 项检查：7 项通过，1 项不通过（警告）"."""
    if not results:
        return "没有数据检查"
    parts = []
    for status in ("passed", "failed", "error", "skipped"):
        these = [r for r in results if r["status"] == status]
        if these:
            soft = _soft(these) if status in ("failed", "error") else ""
            if status == "passed":
                known = sum(1 for r in these if (r.get("known") or {}).get("count"))
                soft = f"（其中 {known} 项只有已知差异）" if known else ""
            parts.append(f"{len(these)} 项{STATUS_TEXT[status]}{soft}")
    return f"{len(results)} 项检查：" + "，".join(parts)


def fmt(value: Any) -> str:
    """A value as people read it: numbers with thousands separators."""
    if value is None:
        return "空值"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        text = f"{value:,.6f}".rstrip("0").rstrip(".")
        return text if text not in ("0", "-0") or value == 0 else f"{value:g}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else fmt(value)
    return text.replace("|", "\\|").replace("\n", " ") or " "


def _md_table(columns: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(_cell(c) for c in columns) + " |"]
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines


def _shown(r: dict[str, Any]) -> str:
    text = STATUS_TEXT[r["status"]]
    if r["status"] in ("failed", "error") and r["severity"] != "error":
        text += f"（{SEVERITY_TEXT[r['severity']]}）"
    return text


def cell_text(value: Any) -> Any:
    """A value in an example row: numbers as they are, without thousands separators
    (jadeflow retest S-15: the year 2026 read 2,026)."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return fmt(value).replace(",", "")
    return value


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    """An example row as a table row: a golden difference gets its key and each column
    as "result / old system"."""
    if not (isinstance(row.get("key"), dict) and row.get("kind") in DIFFERENCE):
        return {c: cell_text(v) for c, v in row.items()}
    kind = row["kind"]
    out: dict[str, Any] = {"差异": DIFFERENCE[kind]}
    out |= {c: cell_text(v) for c, v in row["key"].items()}
    if kind == "duplicate":
        out["哪边"] = "结果表" if row.get("side") == "table" else "老系统"
        out["次数"] = row.get("count")
        return out
    mine, old = row.get("table") or {}, row.get("expected") or {}
    differs = set(row.get("differs") or [])

    def said(value: Any) -> str:
        return fmt(cell_text(value))

    for column in dict.fromkeys([*mine, *old]):
        if kind == "missing":
            out[column] = f"— / {said(old.get(column))}"
        elif kind == "extra":
            out[column] = f"{said(mine.get(column))} / —"
        elif column in differs:
            out[column] = f"{said(mine.get(column))} / {said(old.get(column))}"
        else:
            out[column] = said(mine.get(column))
    return out


def report(results: list[dict[str, Any]]) -> str:
    """The checks as a person reads them (Markdown): one line each, examples after."""
    lines = ["## 数据检查", "", summary(results)]
    if not results:
        return "\n".join(lines)
    lines += [""]
    lines += _md_table(
        ["结果", "检查", "说明"], [[_shown(r), r["label"], r["message"]] for r in results]
    )
    for r in results:
        if not r["rows"] or r["status"] == "passed":
            continue
        rows = [_flat(row) for row in r["rows"]]
        columns = list(dict.fromkeys(c for row in rows for c in row))
        first = [c for c in r.get("focus") or [] if c in columns]
        rest = [c for c in columns if c not in first]
        shown = first + rest[: max(0, REPORT_COLUMNS - len(first))]
        lines += ["", f"### {_shown(r)}：{r['label']}（{len(rows)} 行例子）", ""]
        if r["kind"] == "golden" and any("差异" in row for row in rows):
            lines += ["列里的值是“结果表 / 老系统”。", ""]
        lines += _md_table(shown, [[row.get(c) for c in shown] for row in rows])
        if len(columns) > len(shown):
            why = "：先列对齐的键、不一致的列和检查条件用到的列" if first else ""
            lines += ["", f"共 {len(columns)} 列，只显示 {len(shown)} 列{why}。"]
    return "\n".join(lines)
