"""The platform's own error messages in the reader's language.

The code writes its messages in Chinese. A request whose ``Accept-Language`` prefers
English (the web app sends it from the language switch) gets an error's ``detail`` in
English when ``aep.messages_en`` has the message; otherwise the Chinese is returned as
it is. Values a message carries (names, numbers) are kept, and translated only when a
value is itself one of the catalog's messages (a role, say). Run content is not touched.
"""

import re
from dataclasses import dataclass
from functools import cache

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException
from starlette.responses import Response

from aep.messages_en import MESSAGES

Language = str  # "zh" or "en"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Messages joined into one ("A；B") are translated part by part.
_PARTS = "；"


def language(accept: str | None) -> Language:
    """``en`` when the header ranks English above Chinese, else ``zh``."""
    ranked: list[tuple[float, int, str]] = []
    for i, item in enumerate((accept or "").split(",")):
        tag, _, params = item.strip().partition(";")
        weight = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        primary = tag.strip().lower().split("-")[0]
        if primary in ("en", "zh") and weight > 0:
            ranked.append((-weight, i, primary))
    return min(ranked)[2] if ranked else "zh"


def request_language(request: Request) -> Language:
    return language(request.headers.get("accept-language"))


@dataclass(frozen=True)
class _Template:
    pattern: re.Pattern[str]
    names: tuple[str, ...]
    english: str
    literal: int


@cache
def _templates() -> tuple[_Template, ...]:
    built = []
    for chinese, english in MESSAGES.items():
        names = tuple(_PLACEHOLDER.findall(chinese))
        if not names:
            continue
        pieces = _PLACEHOLDER.split(chinese)
        # split() alternates literal text and names: text, name, text, name, ..., text.
        pattern = "".join(
            re.escape(piece) if i % 2 == 0 else f"(?P<v{i // 2}>.+?)"
            for i, piece in enumerate(pieces)
        )
        literal = len(_PLACEHOLDER.sub("", chinese))
        built.append(_Template(re.compile(f"^{pattern}$", re.DOTALL), names, english, literal))
    # The most literal text first, so 没有应用「{app}」 wins over 没有应用 {app}.
    built.sort(key=lambda template: -template.literal)
    return tuple(built)


def _one(text: str) -> str | None:
    if text in MESSAGES and not _PLACEHOLDER.search(text):
        return MESSAGES[text]
    for template in _templates():
        match = template.pattern.match(text)
        if match:
            values = {
                name: _one(value) or value
                for name, value in zip(
                    template.names,
                    (match[f"v{i}"] for i in range(len(template.names))),
                    strict=True,
                )
            }
            return template.english.format_map(values)
    return None


def translate(text: str, lang: Language) -> str:
    """``text`` in ``lang``; a message the catalog lacks stays Chinese."""
    if lang != "en":
        return text
    whole = _one(text)
    if whole is not None:
        return whole
    if _PARTS in text:
        return "; ".join(_one(part) or part for part in text.split(_PARTS))
    return text


async def translated_http_error(request: Request, exc: Exception) -> Response:
    """FastAPI's error response, with a string ``detail`` in the reader's language."""
    assert isinstance(exc, HTTPException)
    if isinstance(exc.detail, str):
        detail = translate(exc.detail, request_language(request))
        if detail != exc.detail:
            exc = HTTPException(exc.status_code, detail, headers=exc.headers)
    return await http_exception_handler(request, exc)
