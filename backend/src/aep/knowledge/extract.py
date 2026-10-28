"""Blocking document parsers. Call extract() through asyncio.to_thread."""

import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document as WordDocument
from docx.table import Table
from docx.text.paragraph import Paragraph as WordParagraph
from pypdf import PdfReader

from aep.knowledge.store import DocumentKind

NO_TEXT = "没有可提取的文字（可能是扫描件）"


class ExtractError(ValueError):
    pass


@dataclass(frozen=True)
class Paragraph:
    text: str
    heading: str = ""
    page: int | None = None


class _Paragraphs:
    def __init__(self) -> None:
        self.items: list[Paragraph] = []
        self.headings: list[tuple[int, str]] = []

    def title(self, level: int, text: str) -> None:
        self.headings = [(n, title) for n, title in self.headings if n < level]
        self.headings.append((level, text.strip()))

    def add(self, text: str, page: int | None = None) -> None:
        heading = " > ".join(title for _, title in self.headings)
        for part in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
            if part.strip():
                self.items.append(Paragraph(part.strip(), heading, page))


def _decode(path: Path) -> str:
    content = path.read_bytes()
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return content.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ExtractError("文字编码必须为 UTF-8 或 GB18030") from exc


def _markdown(text: str, result: _Paragraphs) -> None:
    buffer: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            buffer.append(line)
            continue
        title = re.match(r"^ {0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*$", line) if not fence else None
        if title:
            result.add("\n".join(buffer))
            buffer = []
            result.title(len(title[1]), title[2])
        else:
            buffer.append(line)
    result.add("\n".join(buffer))


def extract(path: Path, kind: DocumentKind) -> list[Paragraph]:
    """Return body paragraphs in document order, retaining heading paths and PDF pages."""
    result = _Paragraphs()
    if kind == "txt":
        result.add(_decode(path))
    elif kind == "md":
        _markdown(_decode(path), result)
    elif kind == "pdf":
        for number, page in enumerate(PdfReader(path).pages, 1):
            result.add(page.extract_text() or "", page=number)
    elif kind == "docx":
        for item in WordDocument(path).iter_inner_content():
            if isinstance(item, WordParagraph):
                title = re.fullmatch(r"(?:Heading|标题)\s*([1-9])", item.style.name, re.I)
                if title and item.text.strip():
                    result.title(int(title[1]), item.text)
                else:
                    result.add(item.text)
            elif isinstance(item, Table):
                for row in item.rows:
                    cells = [cell.text for cell in row.cells]
                    if any(text.strip() for text in cells):
                        result.add(" | ".join(cells))
    else:
        raise ExtractError("不支持的文档格式")
    if not result.items:
        raise ExtractError(NO_TEXT)
    return result.items
