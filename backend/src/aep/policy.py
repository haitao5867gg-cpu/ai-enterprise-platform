"""The data outbound policy: which models a run may use, and how much of the data read
from data sources goes to them (docs/api/projects.md, "数据外发策略").

The platform applies it in one place, before content goes to a model; nodes do not
each handle it. A *data value* is anything read from a data source or a Databricks
table (rows, samples, value counts, results worked out from them). At ``full`` such
values go to the model as they are; ``masked`` replaces text with stable tokens,
rounds numbers to one significant figure and cuts dates to the month; ``stats`` and
``schema`` send none of them (a sentence says what was left out). People always see
the full values. Tokens in a model's reply are turned back into the values they
stand for before the reply is used, so plans and code name the real values.
"""

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from aep.config import Settings

Samples = Literal["full", "masked", "stats", "schema"]
LEVELS: tuple[Samples, ...] = ("full", "masked", "stats", "schema")
LABELS: dict[str, str] = {
    "full": "完整样本",
    "masked": "脱敏样本",
    "stats": "只发统计",
    "schema": "只发结构",
}
DESCRIPTIONS: dict[str, str] = {
    "full": "样本行和取值分布原样发给模型。",
    "masked": "样本行和取值分布脱敏后发给模型：文字换成记号，数字只留一位有效数字，日期只留到月。",
    "stats": "不发样本行和取值分布，只发空值率、不同值个数和表结构。",
    "schema": "只发表结构：列名、类型和主外键。",
}

# Models the platform runs itself: nothing sent to them leaves the platform.
LOCAL_MODELS = frozenset({"mock-embed", "mock-rerank"})

TOKEN = re.compile(r"‹文本#([0-9a-f]{10})›")
# What a quoted value in error text becomes when the policy sends no data values.
HIDDEN = "‹已隐去›"
# Quoted literals in error text (values a failed run or read may quote), however long,
# across lines, and whatever other quote they hold ("O'Reilly"); a quote right after or
# before a letter is an apostrophe ("doesn't"), not a quote. A quote still open at the
# end of the text (an error cut short) runs to the end.
_QUOTED = re.compile(r"(?<![A-Za-z])(['\"])((?:(?!\1).)+)(?:\1(?![A-Za-z])|\Z)", re.S)
# The JSON object a reply may carry, found the way aep.llm.parse_json finds it.
_FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# Type names errors quote in double quotes ("STRING", "DECIMAL(10,2)"): not values, kept.
_TYPE_NAME = re.compile(r"[A-Z][A-Z0-9_ ]*(\([0-9, ]*\))?")
# Dates and times as connectors write them: kept to the month when masked.
_DATE = re.compile(
    r"^\d{4}-\d{2}(-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?)?(Z|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True)
class DataPolicy:
    samples: Samples = "full"
    # The model aliases a run may use; None: any.
    models: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"samples": self.samples, "models": list(self.models) if self.models else None}

    @classmethod
    def from_dict(cls, value: Any) -> "DataPolicy":
        """A stored policy; runs from before policies existed sent everything."""
        if not isinstance(value, dict):
            return cls()
        samples = value.get("samples")
        models = value.get("models")
        return cls(
            samples if samples in LEVELS else "full",
            tuple(str(m) for m in models) if isinstance(models, list) and models else None,
        )

    def allows(self, model: str) -> bool:
        return self.models is None or model in self.models or model in LOCAL_MODELS


def platform_default(settings: Settings) -> DataPolicy:
    models = tuple(m.strip() for m in settings.allowed_models.split(",") if m.strip())
    return DataPolicy(settings.data_samples, models or None)


def model_problem(
    policy: DataPolicy, model: str, where: str, project: str | None = None
) -> str | None:
    """Why ``model`` may not be used (``where``: the node's title), or None."""
    if policy.allows(model):
        return None
    allowed = "、".join(policy.models or ())
    who = f"项目「{project}」" if project else "平台"
    return f"{who}只允许用模型 {allowed}，节点「{where}」用的是 {model}"


# Masking


def token(run_id: str, text: str) -> str:
    """The stable token a text stands for in one run."""
    digest = hashlib.sha256(f"{run_id}\x00{text}".encode()).hexdigest()
    return f"‹文本#{digest[:10]}›"


def _round(number: int | float) -> int | float:
    if number == 0 or not math.isfinite(number):
        return number
    magnitude = math.floor(math.log10(abs(number)))
    scale = 10.0**magnitude
    rounded = round(number / scale) * scale
    if isinstance(number, int):
        return int(rounded)
    return round(rounded, -magnitude) if magnitude < 0 else rounded


def mask(value: Any, run_id: str) -> Any:
    """``value`` with every text, number and date masked (keys are kept: column names)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return _round(value)
    if isinstance(value, str):
        if _DATE.match(value.strip()):
            return value.strip()[:7]
        return token(run_id, value)
    if isinstance(value, list | tuple):
        return [mask(v, run_id) for v in value]
    if isinstance(value, dict):
        return {k: mask(v, run_id) for k, v in value.items()}
    return token(run_id, str(value))


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _rows(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(row, dict) and all(map(_scalar, row.values())) for row in value)
    )


def left_out(value: Any) -> str:
    """The sentence that stands for data the policy keeps from the model: how many rows
    and which columns when it is a list of rows (or a tool result with ``rows``)."""
    if isinstance(value, dict) and _rows(value.get("rows")):
        value = value["rows"]
    if _rows(value):
        columns: list[str] = []
        for row in value:
            columns += [str(k) for k in row if str(k) not in columns]
        shown = "、".join(columns[:20]) + ("等" if len(columns) > 20 else "")
        return f"（这里有 {len(value)} 行数据，列为 {shown}；按数据外发策略没有发给模型）"
    if value is None or value == "" or value == [] or value == {}:
        return ""
    return "（这里有数据，按数据外发策略没有发给模型）"


def for_model(value: Any, samples: str, run_id: str) -> Any:
    """A data value as the policy lets it go to a model."""
    if samples == "full":
        return value
    if samples == "masked":
        return mask(value, run_id)
    return left_out(value)


def _texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _texts(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _texts(v)
    elif value is not None and not isinstance(value, bool | int | float):
        yield str(value)


def unmask(value: Any, data: Iterable[Any], run_id: str) -> tuple[Any, dict[str, str]]:
    """``value`` (a reply, or what was parsed from it) with the tokens of texts found in
    ``data`` turned back into those texts, and which tokens were (token: text); other
    tokens stay as they are, and so does a token two different texts share."""
    if not any(TOKEN.search(text) for text in _texts(value)):
        return value, {}
    originals: dict[str, str | None] = {}
    for item in data:
        for text in _texts(item):
            key = token(run_id, text)
            # None: two texts share the token, so it stands for neither.
            originals[key] = text if originals.get(key, text) == text else None
    restored: dict[str, str] = {}

    def swap(m: re.Match[str], escaped: bool = False) -> str:
        found = originals.get(m.group(0))
        if found is None:
            return m.group(0)
        restored[m.group(0)] = found
        # Inside a JSON string the text keeps its quotes, backslashes and newlines escaped.
        return json.dumps(found, ensure_ascii=False)[1:-1] if escaped else found

    def text(v: str) -> str:
        span = _json_span(v)
        if span is None:
            return TOKEN.sub(swap, v)
        start, end = span
        inner = TOKEN.sub(lambda m: swap(m, True), v[start:end])
        return TOKEN.sub(swap, v[:start]) + inner + TOKEN.sub(swap, v[end:])

    def back(v: Any) -> Any:
        if isinstance(v, str):
            return text(v)
        if isinstance(v, list):
            return [back(x) for x in v]
        if isinstance(v, dict):
            return {k: back(x) for k, x in v.items()}
        return v

    return back(value), restored


def _json_span(text: str) -> tuple[int, int] | None:
    """Where the JSON object in a reply is, when there is a valid one (tokens in it are
    inside JSON strings: a token is not JSON syntax)."""
    fenced = _FENCED.search(text)
    base = fenced.start(1) if fenced else 0
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        json.loads(candidate[start : end + 1])
    except ValueError:
        return None
    return base + start, base + end + 1


def remask(value: Any, restored: dict[str, str]) -> Any:
    """``value`` with the texts a run turned back from tokens (``restored``) made tokens
    again, before it goes to a model. A text must not touch an ASCII letter, digit or
    underscore on either side; Chinese has no word boundaries, so it is replaced there.
    A text is also found written inside a JSON (or Python) string, with its quotes,
    backslashes and newlines escaped, the way ``unmask`` puts it into a JSON reply."""
    if not restored:
        return value
    tokens: dict[str, str] = {}
    for tok, text in restored.items():
        if text:
            tokens[text] = tok
            tokens.setdefault(json.dumps(text, ensure_ascii=False)[1:-1], tok)
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])(?:"
        + "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
        + r")(?![A-Za-z0-9_])"
    )

    def again(v: Any) -> Any:
        if isinstance(v, str):
            return pattern.sub(lambda m: tokens[m.group(0)], v)
        if isinstance(v, list):
            return [again(x) for x in v]
        if isinstance(v, dict):
            return {k: again(x) for k, x in v.items()}
        return v

    return again(value)


# Error text


def _kept(quote: str, inner: str) -> bool:
    return bool((quote == '"' and _TYPE_NAME.fullmatch(inner)) or TOKEN.fullmatch(inner))


def literals(value: Any) -> list[str]:
    """The quoted values in error text that ``scrub`` replaces."""
    return [
        m.group(2)
        for text in _texts(value)
        for m in _QUOTED.finditer(text)
        if not _kept(m.group(1), m.group(2))
    ]


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters without leaving a quoted value open: a value
    the cut goes through is closed right after it (with "…"), so it still counts as a
    value when ``scrub`` and ``literals`` read the text, even with more text after it."""
    if len(text) <= limit:
        return text
    for m in _QUOTED.finditer(text):
        if m.start() >= limit:
            break
        if limit < m.end():
            return text[:limit] + "…" + m.group(1)
    return text[:limit]


def scrub(value: Any, samples: str, run_id: str) -> Any:
    """Error text as the policy lets it go to a model: the values it quotes made tokens
    (``masked``) or hidden (``stats``, ``schema``); the rest of the text stays."""
    if samples == "full":
        return value

    def one(m: re.Match[str]) -> str:
        quote, inner = m.group(1), m.group(2)
        if _kept(quote, inner):
            return m.group(0)
        return quote + (token(run_id, inner) if samples == "masked" else HIDDEN) + quote

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return _QUOTED.sub(one, v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(value)
