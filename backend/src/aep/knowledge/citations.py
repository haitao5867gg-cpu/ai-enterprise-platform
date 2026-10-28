"""Numbered search hits shared by flow nodes and ETL plan references."""

from dataclasses import dataclass

from aep.knowledge.store import Hit


@dataclass(frozen=True)
class IndexedHit(Hit):
    index: int


@dataclass(frozen=True)
class Source(IndexedHit):
    node: str
    node_title: str


def context(hits: list[IndexedHit]) -> str:
    """The citation text that can be inserted directly into a model prompt."""
    parts = []
    for hit in hits:
        page = f"（第 {hit.page} 页）" if hit.page is not None else ""
        parts.append(f"[{hit.index}]《{hit.document_name}》{hit.heading}{page}\n{hit.text}")
    return "\n\n".join(parts)
