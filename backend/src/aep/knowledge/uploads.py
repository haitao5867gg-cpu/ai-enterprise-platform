"""Validate an entire document batch before saving any original files."""

import shutil
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import UploadFile

from aep.knowledge.store import Document, DocumentKind

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 20


class UploadError(ValueError):
    pass


def validate_files(files: list[UploadFile], root: Path, kb_id: str) -> list[Document]:
    documents: list[Document] = []
    for upload in files:
        original = upload.filename or "未命名文件"
        name = Path(original.replace("\\", "/")).name
        suffix = Path(name).suffix.lower()
        if suffix not in {".pdf", ".docx", ".md", ".txt"}:
            raise UploadError(f"文件「{original}」：只支持 .pdf、.docx、.md 和 .txt")
        if "\x00" in name:
            raise UploadError(f"文件「{original}」：文件名含有无效字符")
        if upload.size is not None and upload.size > MAX_FILE_BYTES:
            raise UploadError(f"文件「{original}」：单文件不能超过 20 MB")
        upload.file.seek(0)
        size = len(upload.file.read(MAX_FILE_BYTES + 1))
        upload.file.seek(0)
        if size > MAX_FILE_BYTES:
            raise UploadError(f"文件「{original}」：单文件不能超过 20 MB")
        document_id = str(uuid4())
        documents.append(
            Document(
                document_id,
                kb_id,
                name,
                cast(DocumentKind, suffix[1:]),
                size,
                str(root / kb_id / document_id / name),
            )
        )
    return documents


def save_files(files: list[UploadFile], documents: list[Document]) -> None:
    for upload, document in zip(files, documents, strict=True):
        path = Path(document.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as target:
            shutil.copyfileobj(upload.file, target)


def remove_files(documents: list[Document]) -> None:
    for document in documents:
        shutil.rmtree(Path(document.path).parent, ignore_errors=True)
