"""Eval cases from a spreadsheet: one row per case, columns named after where their
values go (docs/api/evals.md, "从表格导入"). This module only reads the table; the
cases it returns are checked like the ones a builder saves."""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from openpyxl import load_workbook

MAX_BYTES = 5 * 1024 * 1024
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SPLIT = re.compile(r"[;；]")
COLUMNS = (
    "id、title、input.<字段>、expect.<结果路径>、contains.<结果路径>、excludes.<结果路径>、"
    "judge.<结果路径>、answer.<节点>、answer.<节点>.<字段>"
)


class ImportProblem(Exception):
    """The table cannot be imported; the message says why, ``status`` is the HTTP one."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Column:
    kind: str  # id | title | input | expect | contains | excludes | judge | decision | value
    key: str | None = None  # the input key, check path or node
    field: str | None = None  # an answer's field


def column(header: str, n: int) -> Column:
    """What the ``n``-th column (from 1) holds, from its name."""
    name = header.strip()
    wrong = ImportProblem(f"第 {n} 列「{name}」认不出来：列名要写成 {COLUMNS}")
    if name.lower() in ("id", "title"):
        return Column(name.lower())
    prefix, dot, rest = name.partition(".")
    prefix, rest = prefix.strip().lower(), rest.strip()
    if not dot or not rest:
        raise wrong
    if prefix == "input":
        if not KEY.match(rest):
            raise wrong
        return Column("input", rest)
    if prefix in ("expect", "contains", "excludes", "judge"):
        if re.search(r"\s", rest) or rest.startswith(".") or len(rest) > 200:
            raise wrong
        return Column(prefix, rest)
    if prefix == "answer":
        node, dot, answer_field = rest.partition(".")
        if not KEY.match(node) or (dot and not KEY.match(answer_field)):
            raise wrong
        return Column("value", node, answer_field) if dot else Column("decision", node)
    raise wrong


def cell(value: Any) -> Any:
    """A cell as a case value: numbers and true/false as they are (``3.0`` as ``3``),
    dates as ISO text, other text trimmed; None for an empty cell."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _xlsx_rows(data: bytes) -> list[list[Any]]:
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any failure means "unreadable"
        raise ImportProblem(f"读不出这个文件：{str(exc) or type(exc).__name__}") from exc
    try:
        sheet = book.worksheets[0]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()


def _csv_rows(data: bytes) -> list[list[Any]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ImportProblem("读不出这个文件：不是 UTF-8 或 GB18030 编码的 CSV") from exc
    try:
        return [list(row) for row in csv.reader(io.StringIO(text))]
    except csv.Error as exc:
        raise ImportProblem(f"读不出这个文件：{exc}") from exc


def rows(filename: str, data: bytes) -> list[list[Any]]:
    """The table's rows, header first."""
    if len(data) > MAX_BYTES:
        raise ImportProblem("文件不能超过 5 MB", 413)
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "xlsx":
        return _xlsx_rows(data)
    if suffix == "csv":
        return _csv_rows(data)
    raise ImportProblem("只能导入 .xlsx 或 .csv 文件")


def cases(filename: str, data: bytes) -> list[tuple[int, dict[str, Any]]]:
    """``(row number, case)`` for every non-empty row; row 1 is the first under the
    header. Cases are not yet checked against the eval set's rules."""
    table = rows(filename, data)
    while table and all(cell(v) is None for v in table[0]):
        table.pop(0)
    if not table:
        raise ImportProblem("表里没有用例")
    header = table[0]
    columns: list[Column | None] = []
    seen: dict[str, int] = {}
    for n, name in enumerate(header, start=1):
        text = "" if name is None else str(name).strip()
        if not text:
            columns.append(None)  # an unnamed column must stay empty
            continue
        found = column(text, n)
        token = f"{found.kind}:{found.key}:{found.field}"
        if token in seen:
            raise ImportProblem(f"第 {n} 列「{text}」和第 {seen[token]} 列重复了")
        seen[token] = n
        columns.append(found)
    out: list[tuple[int, dict[str, Any]]] = []
    for number, row in enumerate(table[1:], start=1):
        values = [cell(v) for v in row]
        if all(v is None for v in values):
            continue
        case: dict[str, Any] = {"input": {}, "answers": {}, "expect": []}
        for n, value in enumerate(values, start=1):
            if value is None:
                continue
            col = columns[n - 1] if n <= len(columns) else None
            if col is None:
                raise ImportProblem(f"第 {number} 行第 {n} 列有值，但这一列没有列名")
            _put(case, col, value)
        case.setdefault("id", f"row-{number}")
        out.append((number, case))
    if not out:
        raise ImportProblem("表里没有用例")
    return out


def _put(case: dict[str, Any], col: Column, value: Any) -> None:
    if col.kind == "id":
        case["id"] = str(value)
    elif col.kind == "title":
        case["title"] = str(value)
    elif col.kind == "input":
        case["input"][col.key] = value
    elif col.kind == "expect":
        case["expect"].append({"kind": "equals", "path": col.key, "value": value})
    elif col.kind in ("contains", "excludes"):
        words = [w.strip() for w in SPLIT.split(str(value)) if w.strip()]
        if words:
            case["expect"].append({"kind": col.kind, "path": col.key, "values": words})
    elif col.kind == "judge":
        case["expect"].append({"kind": "judge", "path": col.key, "criteria": str(value)})
    elif col.kind == "decision":
        case["answers"].setdefault(col.key, {})["decision"] = str(value)
    else:
        answer = case["answers"].setdefault(col.key, {})
        answer.setdefault("values", {})[col.field] = value
