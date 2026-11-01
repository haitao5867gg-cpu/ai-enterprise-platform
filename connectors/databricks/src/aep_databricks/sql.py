"""Names, literals and the shape check for the expressions callers may pass.

The connector only runs statements it builds itself. Names are checked and quoted with
backticks, literals are escaped, and the expressions callers pass (``where``, ``expr``)
go into parentheses only after the shape check below. The real boundary is the token's
privileges (USE CATALOG, USE SCHEMA, SELECT); this check keeps a bad expression from
turning into another statement, a subquery, or a call that reaches outside the data.
"""

import re
from dataclasses import dataclass

MAX_NAME = 255
MAX_EXPRESSION = 2000


class ConnectorError(ValueError):
    """A safe message that can be returned to an MCP caller."""


# Names


def _part(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(f"{what}不能为空")
    if value != value.strip() or len(value) > MAX_NAME:
        raise ConnectorError(f"{what} {value!r} 写法不对")
    if "`" in value or any(ord(char) < 32 for char in value):
        raise ConnectorError(f"{what} {value!r} 不能有反引号或控制字符")
    return value


def _parts(value: object, count: int, what: str, example: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ConnectorError(f"{what}要写成 {example}")
    parts = tuple(value.split("."))
    if len(parts) != count or not all(parts):
        raise ConnectorError(f"{what}要写成 {example}，收到的是 {value!r}")
    return tuple(_part(part, what) for part in parts)


@dataclass(frozen=True)
class Schema:
    catalog: str
    schema: str

    @classmethod
    def parse(cls, value: object) -> "Schema":
        catalog, schema = _parts(value, 2, "schema ", "catalog.schema 两段")
        return cls(catalog, schema)

    @property
    def name(self) -> str:
        return f"{self.catalog}.{self.schema}"


@dataclass(frozen=True)
class Table:
    catalog: str
    schema: str
    table: str

    @classmethod
    def parse(cls, value: object) -> "Table":
        catalog, schema, table = _parts(value, 3, "表名", "catalog.schema.table 三段")
        return cls(catalog, schema, table)

    @classmethod
    def of(cls, schema: object, table: object) -> "Table":
        found = Schema.parse(schema)
        return cls(found.catalog, found.schema, _part(table, "表名"))

    @property
    def name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.table}"

    @property
    def sql(self) -> str:
        return f"{ident(self.catalog)}.{ident(self.schema)}.{ident(self.table)}"


def column(value: object) -> str:
    return _part(value, "列名")


def columns(values: object, what: str, most: int = 20) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ConnectorError(f"{what}至少要有一列")
    if len(values) > most:
        raise ConnectorError(f"{what}最多 {most} 列")
    found = [column(value) for value in values]
    if len({name.lower() for name in found}) != len(found):
        raise ConnectorError(f"{what}有重复的列")
    return found


def ident(name: str) -> str:
    """A checked name, quoted for Spark SQL."""
    return "`" + _part(name, "名称") + "`"


def literal(text: str) -> str:
    """A Spark SQL string literal (backslash escapes are on by default)."""
    if any(ord(char) < 32 for char in text):
        raise ConnectorError("取值不能有控制字符")
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


# Expressions

# Calls that reach outside the table's data: Java reflection, secrets, network and
# file reads, model calls (ai_*), and IDENTIFIER(), which could name any of them.
DENIED_FUNCTIONS = frozenset(
    {
        "reflect",
        "try_reflect",
        "java_method",
        "secret",
        "try_secret",
        "list_secrets",
        "http_request",
        "read_files",
        "read_kafka",
        "read_kinesis",
        "read_pubsub",
        "read_pulsar",
        "read_statestore",
        "remote_query",
        "vector_search",
        "identifier",
    }
)
# Subqueries read other tables: no SELECT or TABLE, and FROM only inside these calls.
DENIED_WORDS = frozenset({"select", "table", "with"})
FROM_CALLS = frozenset({"extract", "trim", "substring", "substr", "position", "overlay"})

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class _Token:
    kind: str  # word, name (backticked), string, symbol
    text: str


def _tokens(text: str) -> list[_Token]:
    """Spark SQL's tokens, as far as the check needs them: strings with backslash
    escapes (raw strings without), backticked names with doubled backticks, comments."""
    tokens: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        raw = char in "rR" and i + 1 < n and text[i + 1] in "'\""
        if char in "'\"" or raw:
            quote = text[i + 1] if raw else char
            j = i + (2 if raw else 1)
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" and not raw else 1
            if j >= n:
                raise ConnectorError("引号要配对")
            tokens.append(_Token("string", text[i : j + 1]))
            i = j + 1
            continue
        if char == "`":
            j = i + 1
            while True:
                j = text.find("`", j)
                if j < 0:
                    raise ConnectorError("反引号要配对")
                if j + 1 < n and text[j + 1] == "`":
                    j += 2
                    continue
                break
            tokens.append(_Token("name", text[i + 1 : j].replace("``", "`")))
            i = j + 1
            continue
        if text.startswith(("--", "/*", "*/"), i):
            raise ConnectorError("不能有注释")
        if char == ";":
            raise ConnectorError("不能有分号")
        word = _WORD.match(text, i)
        if word:
            tokens.append(_Token("word", word.group()))
            i = word.end()
            continue
        tokens.append(_Token("symbol", char))
        i += 1
    return tokens


def expression_problem(text: object) -> str | None:
    """Why ``text`` cannot go into a statement as an expression, or None."""
    if not isinstance(text, str) or not text.strip():
        return "不能为空"
    if len(text) > MAX_EXPRESSION:
        return f"最多 {MAX_EXPRESSION} 个字符"
    try:
        tokens = _tokens(text)
    except ConnectorError as exc:
        return str(exc)
    # The word before each open parenthesis: which call (or none) it belongs to.
    calls: list[str] = []
    for i, token in enumerate(tokens):
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        before = tokens[i - 1] if i else None
        if token.kind == "symbol" and token.text == "(":
            calls.append(before.text.lower() if before and before.kind == "word" else "")
            continue
        if token.kind == "symbol" and token.text == ")":
            if not calls:
                return "括号要配对"
            calls.pop()
            continue
        if token.kind not in ("word", "name"):
            continue
        low = token.text.lower()
        if token.kind == "word" and low in DENIED_WORDS:
            return f"不能有 {token.text.upper()}（不能读别的表）"
        if token.kind == "word" and low == "from" and (not calls or calls[-1] not in FROM_CALLS):
            return "不能有 FROM（不能读别的表）"
        if following is not None and following.kind == "symbol" and following.text == "(":
            if before is not None and before.kind == "symbol" and before.text == ".":
                return f"不能调用带 catalog 或 schema 的函数 {token.text}"
            if low in DENIED_FUNCTIONS or low.startswith("ai_"):
                return f"不能用函数 {token.text}"
    if calls:
        return "括号要配对"
    return None


def expression(text: object, what: str) -> str:
    """``text`` checked, ready to go into parentheses."""
    problem = expression_problem(text)
    if problem:
        raise ConnectorError(f"{what}写法不对：{problem}")
    assert isinstance(text, str)
    return text.strip()
