"""Numbered search hits shared by flow nodes and ETL plan references, and the ``[n]``
that cite them."""

import re
from dataclasses import dataclass

from aep.knowledge.store import Hit


@dataclass(frozen=True)
class IndexedHit(Hit):
    index: int


@dataclass(frozen=True)
class Source(IndexedHit):
    node: str
    node_title: str


@dataclass(frozen=True)
class CitedSource(Source):
    """A run's source as ``GET /runs/{id}`` shows it; never written into run state."""

    cited: bool


# What Markdown does not show as prose: fenced code (to the end when unclosed), inline
# code, links and images, and link definitions. Indented code blocks are left in: telling
# them from an indented list paragraph needs a Markdown parser, and models fence code.
_NOT_PROSE = re.compile(
    r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}\1[ \t]*$|\Z)"
    r"|(`+)[^`]*?\2"
    r"|!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\([^)\n]*\)"
    r"|^[ \t]{0,3}\[[^\]\n]+\]:[^\n]*$",
    re.M | re.S,
)
# ASCII digits only, as on the run page; no source number has more than 6.
MARKER = re.compile(r"\[([0-9]{1,6})\]")


def context(hits: list[IndexedHit]) -> str:
    """The citation text that can be inserted directly into a model prompt."""
    parts = []
    for hit in hits:
        page = f"（第 {hit.page} 页）" if hit.page is not None else ""
        parts.append(f"[{hit.index}]《{hit.document_name}》{hit.heading}{page}\n{hit.text}")
    return "\n\n".join(parts)


def cited_numbers(text: str) -> set[int]:
    """The ``[n]`` in Markdown prose: what the run page shows as citation tags."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return {int(n) for n in MARKER.findall(_NOT_PROSE.sub(" ", text))}
