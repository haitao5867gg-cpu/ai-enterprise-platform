"""Pure character-based splitting, respecting document headings and PDF pages."""

import re
from dataclasses import dataclass
from itertools import groupby

from aep.knowledge.extract import Paragraph


@dataclass(frozen=True)
class TextChunk:
    text: str
    heading: str
    page: int | None


def chunk(paragraphs: list[Paragraph], size: int = 800, overlap: int = 100) -> list[TextChunk]:
    """Pack paragraphs, then prefer paragraph/sentence/line boundaries before hard cuts.

    Overlap consists of the exact trailing characters of the preceding chunk. It
    never crosses a heading or page, and discarded short chunks stay discarded.
    """
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("切分大小和重叠长度不正确")
    chunks: list[TextChunk] = []
    for (heading, page), group in groupby(paragraphs, key=lambda p: (p.heading, p.page)):
        text = "\n\n".join(p.text for p in group)
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                window = text[start:end]
                for pattern in (r"\n[ \t]*\n", r"[。？！；.!?;]", r"\n"):
                    boundaries = [
                        match.end()
                        for match in re.finditer(pattern, window)
                        if match.end() > overlap
                        and len(re.sub(r"\s", "", window[: match.end()])) >= 20
                    ]
                    if boundaries:
                        end = start + boundaries[-1]
                        break
            piece = text[start:end]
            if len(re.sub(r"\s", "", piece)) >= 20:
                chunks.append(TextChunk(piece, heading, page))
            if end == len(text):
                break
            start = end - overlap
    return chunks
