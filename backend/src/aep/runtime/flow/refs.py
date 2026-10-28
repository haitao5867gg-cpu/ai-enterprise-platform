"""Variable references in a flow app's settings: ``{{node.field}}``.

A reference names a node's output (``{{draft.text}}``), a constant
(``{{env.refund_limit}}``) or a secret (``{{secret.crm_token}}``). In text a
reference is replaced by its value as text; a setting that is exactly one
reference (a tool argument, a code input) gets the value itself.
"""

import json
import re
from collections.abc import Callable, Iterator
from typing import Any

REF = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}")
WHOLE = re.compile(r"^\s*\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}\s*$")

# Node ids a reference may name besides the app's nodes.
ENV = "env"
SECRET = "secret"

Lookup = Callable[[str, str], Any]


def find(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in REF.finditer(text)]


def whole(text: str) -> tuple[str, str] | None:
    """The reference a value consists of, if it is exactly one."""
    m = WHOLE.match(text)
    return (m.group(1), m.group(2)) if m else None


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def render(text: str, lookup: Lookup) -> str:
    return REF.sub(lambda m: as_text(lookup(m.group(1), m.group(2))), text)


def resolve(value: Any, lookup: Lookup) -> Any:
    """A setting's value: references in text replaced, a lone reference by its value."""
    if isinstance(value, str):
        ref = whole(value)
        return lookup(*ref) if ref else render(value, lookup)
    if isinstance(value, list):
        return [resolve(v, lookup) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, lookup) for k, v in value.items()}
    return value


def strings(
    value: Any, loc: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """Every string inside a setting, with where it is."""
    if isinstance(value, str):
        yield loc, value
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from strings(v, (*loc, i))
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, (*loc, k))
