"""Knowledge records with interchangeable in-memory and Postgres stores."""

import asyncio
import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection, errors
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.knowledge.keywords import chunk_keywords
from aep.runtime.store import DEFAULT_TENANT

DocumentKind = Literal["pdf", "docx", "md", "txt"]
DocumentStatus = Literal["queued", "processing", "ready", "error"]
STATUSES = ("queued", "processing", "ready", "error")
MAX_DOCUMENTS = 500


def _now() -> datetime:
    return datetime.now(UTC)


class DuplicateName(Exception):
    pass


class ModelInUse(Exception):
    pass


class DocumentLimit(Exception):
    pass


class MissingBase(Exception):
    pass


class ReplaceKind(Exception):
    """A replacement upload whose file type differs from the document it replaces."""

    def __init__(self, name: str, kind: str) -> None:
        super().__init__(f"「{name}」原来是 {kind}，替换时也要传 {kind}")
        self.name = name
        self.kind = kind


VALIDITIES = ("active", "pending", "expired")
Validity = Literal["active", "pending", "expired"]


def today_in(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()


@dataclass(frozen=True)
class KnowledgeBase:
    kb_id: str
    name: str
    description: str = ""
    embedding_model: str = "mock-embed"
    chunk_size: int = 800
    chunk_overlap: int = 100
    tenant_id: str = DEFAULT_TENANT
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    document_count: int = 0
    chunk_count: int = 0
    status_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STATUSES, 0))
    validity_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(VALIDITIES, 0))
    # When documents were last uploaded, replaced, deleted or had their dates changed.
    docs_changed_at: datetime | None = None


@dataclass(frozen=True)
class Document:
    document_id: str
    kb_id: str
    name: str
    kind: DocumentKind
    size_bytes: int
    path: str
    status: DocumentStatus = "queued"
    error: str | None = None
    chunk_count: int = 0
    tenant_id: str = DEFAULT_TENANT
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    # Internal snapshot: editing a base must not change already queued documents.
    chunk_size: int = 800
    chunk_overlap: int = 100
    owner: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    version: int = 1
    uploaded_by: str | None = None

    def validity(self, on: date) -> Validity:
        if self.valid_from is not None and self.valid_from > on:
            return "pending"
        if self.valid_until is not None and self.valid_until < on:
            return "expired"
        return "active"


@dataclass(frozen=True)
class DocumentVersion:
    version: int
    size_bytes: int
    uploaded_by: str | None
    uploaded_at: datetime


@dataclass(frozen=True)
class HitStats:
    hits_30d: int = 0
    last_hit_at: datetime | None = None


@dataclass(frozen=True)
class Regression:
    kb_id: str
    cases: list[dict[str, Any]] = field(default_factory=list)
    retrieval: dict[str, Any] | None = None
    top_k: int = 5
    min_score: float = 0.3
    updated_by: str | None = None
    updated_at: datetime | None = None
    last_run: dict[str, Any] | None = None


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    kb_id: str
    position: int
    text: str
    heading: str = ""
    page: int | None = None
    embedding: list[float] = field(default_factory=list, repr=False)
    tenant_id: str = DEFAULT_TENANT
    keywords: str | None = None


@dataclass(frozen=True)
class Hit:
    kb_id: str
    kb_name: str
    document_id: str
    document_name: str
    chunk_id: str
    position: int
    text: str
    heading: str
    page: int | None
    score: float
    scores: dict[str, float | None] = field(
        default_factory=lambda: {"vector": None, "keyword": None, "rerank": None}, kw_only=True
    )


class KnowledgeStore(Protocol):
    async def setup(self) -> None: ...
    async def create(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "mock-embed",
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> KnowledgeBase: ...
    async def get(self, kb_id: str) -> KnowledgeBase | None: ...
    async def all(self) -> list[KnowledgeBase]: ...
    async def update(self, kb_id: str, **changes: Any) -> KnowledgeBase | None: ...
    async def delete(self, kb_id: str) -> None: ...
    async def documents(self, kb_id: str) -> list[Document]: ...
    async def add_documents(
        self,
        kb_id: str,
        documents: list[Document],
        *,
        replace: bool = False,
        uploaded_by: str | None = None,
    ) -> list[Document]: ...
    async def get_document(self, kb_id: str, document_id: str) -> Document | None: ...
    async def update_document(
        self, kb_id: str, document_id: str, **changes: Any
    ) -> Document | None: ...
    async def versions(self, document_id: str) -> list[DocumentVersion]: ...
    async def record_hits(self, run_id: str, hits: Iterable[tuple[str, str]]) -> None: ...
    async def hit_stats(self, kb_id: str, since: datetime) -> dict[str, HitStats]: ...
    async def get_regression(self, kb_id: str) -> Regression | None: ...
    async def save_regression(
        self,
        kb_id: str,
        cases: list[dict[str, Any]],
        retrieval: dict[str, Any] | None,
        top_k: int,
        min_score: float,
        updated_by: str | None,
    ) -> Regression: ...
    async def save_regression_run(self, kb_id: str, last_run: dict[str, Any]) -> Regression: ...
    async def delete_document(self, kb_id: str, document_id: str) -> None: ...
    async def chunks(
        self, document_id: str, offset: int, limit: int
    ) -> tuple[int, list[Chunk]]: ...
    async def requeue(self, kb_id: str, document_id: str | None = None) -> int: ...
    async def claim(self) -> Document | None: ...
    async def recover_stale(self, before: datetime) -> int: ...
    async def backfill_keywords(self, limit: int = 500) -> int: ...
    async def touch(self, document: Document) -> Document | None: ...
    async def replace_chunks(self, document: Document, chunks: list[Chunk]) -> None: ...
    async def fail(self, document: Document, error: str) -> None: ...
    async def vector_candidates(
        self, kb_id: str, vector: list[float], n: int, *, include_inactive: bool = False
    ) -> list[str]: ...
    async def keyword_candidates(
        self, kb_id: str, terms: list[str], n: int, *, include_inactive: bool = False
    ) -> list[str]: ...
    async def score_chunks(
        self,
        kb_id: str,
        chunk_ids: list[str],
        vector: list[float] | None,
        terms: list[str] | None,
        *,
        include_inactive: bool = False,
    ) -> list[Hit]: ...
    async def search(
        self, kb_id: str, vector: list[float], top_k: int, min_score: float
    ) -> list[Hit]: ...


def _copy[T](value: T, **changes: Any) -> T:
    # dataclasses.replace, under a name the add_documents ``replace`` flag does not shadow.
    return replace(value, **changes)  # type: ignore[type-var]


def _replaced(
    old: Document, path: str, size: int, base: KnowledgeBase, uploaded_by: str | None
) -> Document:
    return _copy(
        old,
        path=path,
        size_bytes=size,
        version=old.version + 1,
        status="queued",
        error=None,
        chunk_count=0,
        chunk_size=base.chunk_size,
        chunk_overlap=base.chunk_overlap,
        uploaded_by=uploaded_by,
        updated_at=_now(),
    )


def _unique_filename(name: str, taken: set[str]) -> str:
    candidate, number = name, 1
    path = Path(name)
    while candidate in taken:
        number += 1
        candidate = f"{path.stem} ({number}){path.suffix}"
    taken.add(candidate)
    return candidate


class MemoryKnowledgeStore:
    """In-process store for tests; mutations do not yield partway through a batch."""

    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.timezone = timezone
        self._bases: dict[str, KnowledgeBase] = {}
        self._documents: dict[str, Document] = {}
        self._chunks: dict[str, list[Chunk]] = {}
        self._versions: dict[str, list[DocumentVersion]] = {}
        self._hits: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._regressions: dict[str, Regression] = {}

    async def setup(self) -> None:
        return None

    def _view(self, base: KnowledgeBase) -> KnowledgeBase:
        documents = [d for d in self._documents.values() if d.kb_id == base.kb_id]
        today = today_in(self.timezone)
        return replace(
            base,
            document_count=len(documents),
            chunk_count=sum(d.chunk_count for d in documents),
            status_counts={
                status: sum(d.status == status for d in documents) for status in STATUSES
            },
            validity_counts={
                state: sum(d.validity(today) == state for d in documents) for state in VALIDITIES
            },
        )

    def _changed(self, kb_id: str) -> None:
        if kb_id in self._bases:
            self._bases[kb_id] = replace(self._bases[kb_id], docs_changed_at=_now())

    def _active(self, document: Document, include_inactive: bool) -> bool:
        return include_inactive or document.validity(today_in(self.timezone)) == "active"

    async def create(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "mock-embed",
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> KnowledgeBase:
        if any(b.name == name for b in self._bases.values()):
            raise DuplicateName(name)
        base = KnowledgeBase(
            str(uuid.uuid4()), name, description, embedding_model, chunk_size, chunk_overlap
        )
        self._bases[base.kb_id] = base
        return base

    async def get(self, kb_id: str) -> KnowledgeBase | None:
        base = self._bases.get(kb_id)
        return self._view(base) if base else None

    async def all(self) -> list[KnowledgeBase]:
        return [self._view(b) for b in sorted(self._bases.values(), key=lambda b: b.name)]

    async def update(self, kb_id: str, **changes: Any) -> KnowledgeBase | None:
        base = self._bases.get(kb_id)
        if base is None:
            return None
        name = changes.get("name", base.name)
        if any(b.name == name and b.kb_id != kb_id for b in self._bases.values()):
            raise DuplicateName(name)
        if changes.get("embedding_model", base.embedding_model) != base.embedding_model and any(
            d.kb_id == kb_id for d in self._documents.values()
        ):
            raise ModelInUse(kb_id)
        base = replace(base, **changes, updated_at=_now())
        self._bases[kb_id] = base
        return self._view(base)

    async def delete(self, kb_id: str) -> None:
        self._bases.pop(kb_id, None)
        self._regressions.pop(kb_id, None)
        for key, document in list(self._documents.items()):
            if document.kb_id == kb_id:
                self._drop_document(key)

    def _drop_document(self, document_id: str) -> None:
        self._documents.pop(document_id, None)
        self._chunks.pop(document_id, None)
        self._versions.pop(document_id, None)
        for key in [key for key in self._hits if key[1] == document_id]:
            self._hits.pop(key)

    async def documents(self, kb_id: str) -> list[Document]:
        return sorted(
            [d for d in self._documents.values() if d.kb_id == kb_id],
            key=lambda d: (d.created_at, d.document_id),
            reverse=True,
        )

    async def add_documents(
        self,
        kb_id: str,
        documents: list[Document],
        *,
        replace: bool = False,
        uploaded_by: str | None = None,
    ) -> list[Document]:
        if kb_id not in self._bases:
            raise MissingBase(kb_id)
        existing = {d.name: d for d in self._documents.values() if d.kb_id == kb_id}
        replaced = [d for d in documents if replace and d.name in existing]
        for document in replaced:
            if document.kind != existing[document.name].kind:
                raise ReplaceKind(document.name, existing[document.name].kind)
        taken = set(existing)
        if len(taken) + len(documents) - len(replaced) > MAX_DOCUMENTS:
            raise DocumentLimit("一个知识库最多 500 个文档")
        base = self._bases[kb_id]
        added = []
        for d in documents:
            old = existing.get(d.name) if replace else None
            if old is not None:
                if not self._versions.get(old.document_id):
                    self._versions[old.document_id] = [
                        DocumentVersion(
                            old.version, old.size_bytes, old.uploaded_by, old.created_at
                        )
                    ]
                new = _replaced(old, d.path, d.size_bytes, base, uploaded_by)
                self._chunks.pop(old.document_id, None)
                existing[d.name] = new
            else:
                new = _copy(
                    d,
                    name=_unique_filename(d.name, taken),
                    chunk_size=base.chunk_size,
                    chunk_overlap=base.chunk_overlap,
                    uploaded_by=uploaded_by,
                )
            self._documents[new.document_id] = new
            self._versions.setdefault(new.document_id, []).append(
                DocumentVersion(new.version, new.size_bytes, uploaded_by, new.updated_at)
            )
            added.append(new)
        self._changed(kb_id)
        return added

    async def get_document(self, kb_id: str, document_id: str) -> Document | None:
        document = self._documents.get(document_id)
        return document if document and document.kb_id == kb_id else None

    async def update_document(
        self, kb_id: str, document_id: str, **changes: Any
    ) -> Document | None:
        if changes.keys() - {"owner", "valid_from", "valid_until"}:
            raise ValueError("未知的文档字段")
        document = await self.get_document(kb_id, document_id)
        if document is None:
            return None
        document = _copy(document, **changes)
        self._documents[document_id] = document
        if changes.keys() & {"valid_from", "valid_until"}:
            self._changed(kb_id)
        return document

    async def versions(self, document_id: str) -> list[DocumentVersion]:
        return sorted(self._versions.get(document_id, []), key=lambda v: -v.version)

    async def record_hits(self, run_id: str, hits: Iterable[tuple[str, str]]) -> None:
        for kb_id, document_id in hits:
            if document_id in self._documents:
                self._hits.setdefault((run_id, document_id), (kb_id, _now()))

    async def hit_stats(self, kb_id: str, since: datetime) -> dict[str, HitStats]:
        found: dict[str, HitStats] = {}
        for (_, document_id), (kb, at) in self._hits.items():
            if kb != kb_id:
                continue
            stats = found.get(document_id, HitStats())
            found[document_id] = HitStats(
                stats.hits_30d + (at >= since),
                max(at, stats.last_hit_at) if stats.last_hit_at else at,
            )
        return found

    async def get_regression(self, kb_id: str) -> Regression | None:
        return self._regressions.get(kb_id)

    async def save_regression(
        self,
        kb_id: str,
        cases: list[dict[str, Any]],
        retrieval: dict[str, Any] | None,
        top_k: int,
        min_score: float,
        updated_by: str | None,
    ) -> Regression:
        if kb_id not in self._bases:
            raise MissingBase(kb_id)
        old = self._regressions.get(kb_id)
        record = Regression(
            kb_id,
            cases,
            retrieval,
            top_k,
            min_score,
            updated_by,
            _now(),
            old.last_run if old else None,
        )
        self._regressions[kb_id] = record
        return record

    async def save_regression_run(self, kb_id: str, last_run: dict[str, Any]) -> Regression:
        old = self._regressions.get(kb_id)
        if old is None or kb_id not in self._bases:
            raise MissingBase(kb_id)
        record = _copy(old, last_run=last_run)
        self._regressions[kb_id] = record
        return record

    async def delete_document(self, kb_id: str, document_id: str) -> None:
        if await self.get_document(kb_id, document_id):
            self._drop_document(document_id)
            self._changed(kb_id)

    async def chunks(self, document_id: str, offset: int, limit: int) -> tuple[int, list[Chunk]]:
        chunks = sorted(self._chunks.get(document_id, []), key=lambda c: c.position)
        return len(chunks), chunks[offset : offset + limit]

    async def requeue(self, kb_id: str, document_id: str | None = None) -> int:
        base = self._bases.get(kb_id)
        if base is None:
            return 0
        count = 0
        for key, document in self._documents.items():
            if document.kb_id == kb_id and (document_id is None or key == document_id):
                self._documents[key] = replace(
                    document,
                    status="queued",
                    error=None,
                    chunk_count=0,
                    updated_at=_now(),
                    chunk_size=base.chunk_size,
                    chunk_overlap=base.chunk_overlap,
                )
                self._chunks.pop(key, None)
                count += 1
        return count

    async def claim(self) -> Document | None:
        queued = sorted(
            (d for d in self._documents.values() if d.status == "queued"),
            key=lambda d: (d.created_at, d.document_id),
        )
        if not queued:
            return None
        document = replace(queued[0], status="processing", error=None, updated_at=_now())
        self._documents[document.document_id] = document
        return document

    async def recover_stale(self, before: datetime) -> int:
        count = 0
        for key, document in self._documents.items():
            if document.status == "processing" and document.updated_at < before:
                self._documents[key] = replace(
                    document, status="queued", error=None, chunk_count=0, updated_at=_now()
                )
                self._chunks.pop(key, None)
                count += 1
        return count

    async def backfill_keywords(self, limit: int = 500) -> int:
        selected = [
            chunk for chunks in self._chunks.values() for chunk in chunks if chunk.keywords is None
        ][:limit]
        values = await asyncio.to_thread(
            lambda: {c.chunk_id: chunk_keywords(c.text, c.heading) for c in selected}
        )
        for document_id, chunks in self._chunks.items():
            self._chunks[document_id] = [
                replace(c, keywords=values[c.chunk_id])
                if c.keywords is None and c.chunk_id in values
                else c
                for c in chunks
            ]
        return len(selected)

    def _owns_claim(self, document: Document) -> bool:
        current = self._documents.get(document.document_id)
        return bool(
            current and current.status == "processing" and current.updated_at == document.updated_at
        )

    async def touch(self, document: Document) -> Document | None:
        if not self._owns_claim(document):
            return None
        document = replace(document, updated_at=_now())
        self._documents[document.document_id] = document
        return document

    async def replace_chunks(self, document: Document, chunks: list[Chunk]) -> None:
        if self._owns_claim(document):
            self._chunks[document.document_id] = list(chunks)
            self._documents[document.document_id] = replace(
                document, status="ready", chunk_count=len(chunks), error=None, updated_at=_now()
            )

    async def fail(self, document: Document, error: str) -> None:
        if self._owns_claim(document):
            self._chunks.pop(document.document_id, None)
            self._documents[document.document_id] = replace(
                document, status="error", error=error, chunk_count=0, updated_at=_now()
            )

    async def vector_candidates(
        self, kb_id: str, vector: list[float], n: int, *, include_inactive: bool = False
    ) -> list[str]:
        ids = [c.chunk_id for chunks in self._chunks.values() for c in chunks if c.kb_id == kb_id]
        hits = await self.score_chunks(kb_id, ids, vector, None, include_inactive=include_inactive)
        return [h.chunk_id for h in sorted(hits, key=lambda h: (-h.score, h.chunk_id))[:n]]

    async def keyword_candidates(
        self, kb_id: str, terms: list[str], n: int, *, include_inactive: bool = False
    ) -> list[str]:
        terms = _query_terms(terms)
        if not terms:
            return []
        ids = [c.chunk_id for chunks in self._chunks.values() for c in chunks if c.kb_id == kb_id]
        hits = await self.score_chunks(kb_id, ids, None, terms, include_inactive=include_inactive)
        return [
            h.chunk_id
            for h in sorted(hits, key=lambda h: (-h.score, h.chunk_id))[:n]
            if h.score > 0
        ]

    async def score_chunks(
        self,
        kb_id: str,
        chunk_ids: list[str],
        vector: list[float] | None,
        terms: list[str] | None,
        *,
        include_inactive: bool = False,
    ) -> list[Hit]:
        base = self._bases.get(kb_id)
        if base is None:
            return []
        selected = set(chunk_ids)
        wanted = set(terms or [])
        hits: list[Hit] = []
        for document in self._documents.values():
            if (
                document.kb_id != kb_id
                or document.status != "ready"
                or not self._active(document, include_inactive)
            ):
                continue
            for chunk in self._chunks.get(document.document_id, []):
                if chunk.chunk_id not in selected:
                    continue
                vector_score = _cosine(vector, chunk.embedding) if vector is not None else None
                keyword_score = None
                if terms is not None:
                    keyword_score = (
                        len(wanted.intersection((chunk.keywords or "").split())) / len(wanted)
                        if wanted
                        else 0.0
                    )
                hits.append(
                    Hit(
                        kb_id,
                        base.name,
                        document.document_id,
                        document.name,
                        chunk.chunk_id,
                        chunk.position,
                        chunk.text,
                        chunk.heading,
                        chunk.page,
                        vector_score if vector_score is not None else keyword_score or 0.0,
                        scores={"vector": vector_score, "keyword": keyword_score, "rerank": None},
                    )
                )
        return hits

    async def search(
        self, kb_id: str, vector: list[float], top_k: int, min_score: float
    ) -> list[Hit]:
        ids = await self.vector_candidates(kb_id, vector, top_k)
        hits = [
            h for h in await self.score_chunks(kb_id, ids, vector, None) if h.score >= min_score
        ]
        return sorted(hits, key=lambda h: (-h.score, h.chunk_id))[:top_k]


SCHEMA = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
CREATE TABLE IF NOT EXISTS knowledge_bases (
    kb_id uuid PRIMARY KEY,
    tenant_id text NOT NULL DEFAULT 'default',
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    embedding_model text NOT NULL,
    chunk_size integer NOT NULL,
    chunk_overlap integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
)""",
    """
CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id uuid PRIMARY KEY,
    kb_id uuid NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
    tenant_id text NOT NULL DEFAULT 'default',
    name text NOT NULL,
    kind text NOT NULL,
    size_bytes bigint NOT NULL,
    path text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    error text,
    chunk_count integer NOT NULL DEFAULT 0,
    chunk_size integer NOT NULL,
    chunk_overlap integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kb_id, name)
)""",
    """
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
    kb_id uuid NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
    tenant_id text NOT NULL DEFAULT 'default',
    position integer NOT NULL,
    text text NOT NULL,
    heading text NOT NULL DEFAULT '',
    page integer,
    embedding vector(1024) NOT NULL,
    UNIQUE (document_id, position)
)""",
    "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx"
    " ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS knowledge_documents_queue_idx"
    " ON knowledge_documents (status, updated_at)",
    "CREATE INDEX IF NOT EXISTS knowledge_documents_kb_idx ON knowledge_documents (kb_id)",
    "CREATE INDEX IF NOT EXISTS knowledge_chunks_kb_idx ON knowledge_chunks (kb_id)",
    "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS keywords text",
    "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS tsv tsvector"
    " GENERATED ALWAYS AS (to_tsvector('simple', coalesce(keywords, ''))) STORED",
    "CREATE INDEX IF NOT EXISTS knowledge_chunks_tsv_idx ON knowledge_chunks USING gin (tsv)",
    # Governance (docs/api/knowledge-governance.md).
    "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS docs_changed_at timestamptz",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS owner text",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS valid_from date",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS valid_until date",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS uploaded_by text",
    """
CREATE TABLE IF NOT EXISTS knowledge_document_versions (
    document_id uuid NOT NULL REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
    tenant_id text NOT NULL DEFAULT 'default',
    version integer NOT NULL,
    size_bytes bigint NOT NULL,
    uploaded_by text,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, version)
)""",
    """
CREATE TABLE IF NOT EXISTS knowledge_hits (
    tenant_id text NOT NULL DEFAULT 'default',
    run_id text NOT NULL,
    document_id uuid NOT NULL REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
    kb_id uuid NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
    at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, document_id)
)""",
    "CREATE INDEX IF NOT EXISTS knowledge_hits_kb_idx ON knowledge_hits (kb_id, at)",
    "CREATE INDEX IF NOT EXISTS knowledge_hits_document_idx ON knowledge_hits (document_id)",
    """
CREATE TABLE IF NOT EXISTS knowledge_regressions (
    kb_id uuid PRIMARY KEY REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
    tenant_id text NOT NULL DEFAULT 'default',
    cases jsonb NOT NULL DEFAULT '[]',
    retrieval jsonb,
    top_k integer NOT NULL DEFAULT 5,
    min_score double precision NOT NULL DEFAULT 0.3,
    updated_by text,
    updated_at timestamptz,
    last_run jsonb
)""",
]

BASE_SELECT = """
SELECT b.*, b.kb_id::text AS kb_id,
       count(d.document_id)::integer AS document_count,
       coalesce(sum(d.chunk_count), 0)::integer AS chunk_count,
       jsonb_build_object(
           'queued', count(*) FILTER (WHERE d.status = 'queued'),
           'processing', count(*) FILTER (WHERE d.status = 'processing'),
           'ready', count(*) FILTER (WHERE d.status = 'ready'),
           'error', count(*) FILTER (WHERE d.status = 'error')
       ) AS status_counts,
       jsonb_build_object(
           'pending', count(*) FILTER (WHERE d.valid_from > %(today)s),
           'expired', count(*) FILTER (
               WHERE d.valid_until < %(today)s
               AND (d.valid_from IS NULL OR d.valid_from <= %(today)s))
       ) AS validity_counts
FROM knowledge_bases b LEFT JOIN knowledge_documents d ON b.kb_id = d.kb_id
WHERE b.tenant_id = %(tenant)s
"""
DOCUMENT_COLUMNS = (
    "document_id::text, kb_id::text, tenant_id, name, kind, size_bytes, path, status,"
    " error, chunk_count, created_at, updated_at, chunk_size, chunk_overlap,"
    " owner, valid_from, valid_until, version, uploaded_by"
)
# Only documents in effect on %(today)s; d is knowledge_documents.
ACTIVE = (
    " AND (d.valid_from IS NULL OR d.valid_from <= %(today)s)"
    " AND (d.valid_until IS NULL OR d.valid_until >= %(today)s)"
)
REGRESSION_COLUMNS = (
    "kb_id::text, cases, retrieval, top_k, min_score, updated_by, updated_at, last_run"
)


def _base_row(row: dict[str, Any]) -> KnowledgeBase:
    counts = row.pop("validity_counts")
    active = row["document_count"] - counts["pending"] - counts["expired"]
    return KnowledgeBase(**row, validity_counts={"active": active, **counts})


CHUNK_COLUMNS = (
    "chunk_id::text, document_id::text, kb_id::text, tenant_id, position, text, heading, page,"
    " keywords"
)


def _cosine(vector: list[float], other: list[float]) -> float:
    norm = math.sqrt(sum(v * v for v in vector))
    other_norm = math.sqrt(sum(v * v for v in other))
    score = (
        sum(a * b for a, b in zip(vector, other, strict=True)) / (norm * other_norm)
        if norm and other_norm
        else 0.0
    )
    return max(0.0, min(1.0, score))


def _query_terms(terms: list[str]) -> list[str]:
    # OR is websearch syntax rather than a searchable term.
    return [term for term in dict.fromkeys(terms) if term and term.lower() != "or"]


KEYWORD_SCORE = (
    "(SELECT count(*)::float8 FROM unnest(%(words)s::text[]) AS q(term)"
    " WHERE q.term = ANY(string_to_array(coalesce(c.keywords, ''), ' ')))"
    " / greatest(cardinality(%(words)s::text[]), 1)"
)


def _key(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _vector(values: list[float]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


class PostgresKnowledgeStore:
    """Postgres/pgvector store. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool, timezone: str = "Asia/Shanghai") -> None:
        self._pool = pool
        self.timezone = timezone

    def _today(self) -> date:
        return today_in(self.timezone)

    def _active(self, include_inactive: bool) -> str:
        return "" if include_inactive else ACTIVE

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def _get(self, conn: AsyncConnection, kb_id: str) -> KnowledgeBase | None:
        cur = await conn.execute(
            BASE_SELECT + " AND b.kb_id = %(kb)s GROUP BY b.kb_id",
            {"today": self._today(), "tenant": DEFAULT_TENANT, "kb": _key(kb_id)},
        )
        row = await cur.fetchone()
        return _base_row(row) if row else None

    async def create(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "mock-embed",
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> KnowledgeBase:
        key = str(uuid.uuid4())
        async with self._pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO knowledge_bases"
                    " (kb_id, name, description, embedding_model, chunk_size, chunk_overlap)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (key, name, description, embedding_model, chunk_size, chunk_overlap),
                )
            except errors.UniqueViolation as exc:
                raise DuplicateName(name) from exc
            return await self._get(conn, key)

    async def get(self, kb_id: str) -> KnowledgeBase | None:
        async with self._pool.connection() as conn:
            return await self._get(conn, kb_id)

    async def all(self) -> list[KnowledgeBase]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                BASE_SELECT + " GROUP BY b.kb_id ORDER BY b.name",
                {"today": self._today(), "tenant": DEFAULT_TENANT},
            )
            return [_base_row(row) for row in await cur.fetchall()]

    async def update(self, kb_id: str, **changes: Any) -> KnowledgeBase | None:
        allowed = {"name", "description", "embedding_model", "chunk_size", "chunk_overlap"}
        if changes.keys() - allowed:
            raise ValueError("未知的知识库字段")
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT embedding_model FROM knowledge_bases"
                " WHERE kb_id = %s AND tenant_id = %s FOR UPDATE",
                (_key(kb_id), DEFAULT_TENANT),
            )
            base = await cur.fetchone()
            if base is None:
                return None
            if changes.get("embedding_model", base["embedding_model"]) != base["embedding_model"]:
                cur = await conn.execute(
                    "SELECT 1 FROM knowledge_documents WHERE kb_id = %s LIMIT 1", (_key(kb_id),)
                )
                if await cur.fetchone():
                    raise ModelInUse(kb_id)
            assignments = [f"{key} = %s" for key in changes]
            assignments.append("updated_at = now()")
            try:
                await conn.execute(
                    "UPDATE knowledge_bases SET " + ", ".join(assignments) + " WHERE kb_id = %s",
                    (*changes.values(), _key(kb_id)),
                )
            except errors.UniqueViolation as exc:
                raise DuplicateName(changes.get("name")) from exc
            return await self._get(conn, kb_id)

    async def delete(self, kb_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM knowledge_bases WHERE kb_id = %s AND tenant_id = %s",
                (_key(kb_id), DEFAULT_TENANT),
            )

    async def documents(self, kb_id: str) -> list[Document]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {DOCUMENT_COLUMNS} FROM knowledge_documents"
                " WHERE kb_id = %s AND tenant_id = %s ORDER BY created_at DESC, document_id DESC",
                (_key(kb_id), DEFAULT_TENANT),
            )
            return [Document(**row) for row in await cur.fetchall()]

    async def add_documents(
        self,
        kb_id: str,
        documents: list[Document],
        *,
        replace: bool = False,
        uploaded_by: str | None = None,
    ) -> list[Document]:
        # Serialize uploads and model edits on the base, including across API replicas.
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT chunk_size, chunk_overlap FROM knowledge_bases"
                " WHERE kb_id = %s AND tenant_id = %s FOR UPDATE",
                (_key(kb_id), DEFAULT_TENANT),
            )
            base = await cur.fetchone()
            if base is None:
                raise MissingBase(kb_id)
            cur = await conn.execute(
                "SELECT document_id, name, kind FROM knowledge_documents WHERE kb_id = %s",
                (_key(kb_id),),
            )
            existing = {row["name"]: row for row in await cur.fetchall()}
            replaced = [d for d in documents if replace and d.name in existing]
            for document in replaced:
                if document.kind != existing[document.name]["kind"]:
                    raise ReplaceKind(document.name, existing[document.name]["kind"])
            taken = set(existing)
            if len(taken) + len(documents) - len(replaced) > MAX_DOCUMENTS:
                raise DocumentLimit("一个知识库最多 500 个文档")
            added: list[Document] = []
            for document in documents:
                old = existing.get(document.name) if replace else None
                if old is not None:
                    # Documents from before versions were kept get their first version now.
                    await conn.execute(
                        "INSERT INTO knowledge_document_versions"
                        " (document_id, version, size_bytes, uploaded_by, uploaded_at)"
                        " SELECT document_id, version, size_bytes, uploaded_by, created_at"
                        " FROM knowledge_documents WHERE document_id = %s"
                        " ON CONFLICT DO NOTHING",
                        (old["document_id"],),
                    )
                    cur = await conn.execute(
                        "UPDATE knowledge_documents SET path = %s, size_bytes = %s,"
                        " version = version + 1, status = 'queued', error = NULL,"
                        " chunk_count = 0, chunk_size = %s, chunk_overlap = %s,"
                        " uploaded_by = %s, updated_at = now()"
                        f" WHERE document_id = %s RETURNING {DOCUMENT_COLUMNS}",
                        (
                            document.path,
                            document.size_bytes,
                            base["chunk_size"],
                            base["chunk_overlap"],
                            uploaded_by,
                            old["document_id"],
                        ),
                    )
                    await conn.execute(
                        "DELETE FROM knowledge_chunks WHERE document_id = %s",
                        (old["document_id"],),
                    )
                else:
                    name = _unique_filename(document.name, taken)
                    cur = await conn.execute(
                        "INSERT INTO knowledge_documents (document_id, kb_id, name, kind,"
                        " size_bytes, path, chunk_size, chunk_overlap, uploaded_by)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        f" RETURNING {DOCUMENT_COLUMNS}",
                        (
                            document.document_id,
                            kb_id,
                            name,
                            document.kind,
                            document.size_bytes,
                            document.path,
                            base["chunk_size"],
                            base["chunk_overlap"],
                            uploaded_by,
                        ),
                    )
                row = Document(**await cur.fetchone())
                await conn.execute(
                    "INSERT INTO knowledge_document_versions"
                    " (document_id, version, size_bytes, uploaded_by)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (_key(row.document_id), row.version, row.size_bytes, uploaded_by),
                )
                added.append(row)
            await self._changed(conn, kb_id)
            return added

    async def _changed(self, conn: AsyncConnection, kb_id: str) -> None:
        await conn.execute(
            "UPDATE knowledge_bases SET docs_changed_at = now() WHERE kb_id = %s",
            (_key(kb_id),),
        )

    async def update_document(
        self, kb_id: str, document_id: str, **changes: Any
    ) -> Document | None:
        if changes.keys() - {"owner", "valid_from", "valid_until"}:
            raise ValueError("未知的文档字段")
        async with self._pool.connection() as conn, conn.transaction():
            assignments = [f"{key} = %s" for key in changes] or ["owner = owner"]
            cur = await conn.execute(
                "UPDATE knowledge_documents SET "
                + ", ".join(assignments)
                + " WHERE kb_id = %s AND document_id = %s AND tenant_id = %s"
                f" RETURNING {DOCUMENT_COLUMNS}",
                (*changes.values(), _key(kb_id), _key(document_id), DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            if changes.keys() & {"valid_from", "valid_until"}:
                await self._changed(conn, kb_id)
            return Document(**row)

    async def versions(self, document_id: str) -> list[DocumentVersion]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT version, size_bytes, uploaded_by, uploaded_at"
                " FROM knowledge_document_versions WHERE document_id = %s AND tenant_id = %s"
                " ORDER BY version DESC",
                (_key(document_id), DEFAULT_TENANT),
            )
            return [DocumentVersion(**row) for row in await cur.fetchall()]

    async def record_hits(self, run_id: str, hits: Iterable[tuple[str, str]]) -> None:
        rows = [(run_id, _key(doc), _key(kb)) for kb, doc in dict.fromkeys(hits)]
        if not rows:
            return
        async with self._pool.connection() as conn, conn.cursor() as cur:
            # A document deleted meanwhile is skipped rather than failing the batch.
            await cur.executemany(
                "INSERT INTO knowledge_hits (run_id, document_id, kb_id)"
                " SELECT %s, %s, %s WHERE EXISTS (SELECT 1 FROM knowledge_documents"
                " WHERE document_id = %s) ON CONFLICT DO NOTHING",
                [(*row, row[1]) for row in rows],
            )

    async def hit_stats(self, kb_id: str, since: datetime) -> dict[str, HitStats]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT document_id::text, count(*) FILTER (WHERE at >= %s)::integer AS hits_30d,"
                " max(at) AS last_hit_at FROM knowledge_hits"
                " WHERE kb_id = %s AND tenant_id = %s GROUP BY document_id",
                (since, _key(kb_id), DEFAULT_TENANT),
            )
            return {row.pop("document_id"): HitStats(**row) for row in await cur.fetchall()}

    async def get_regression(self, kb_id: str) -> Regression | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {REGRESSION_COLUMNS} FROM knowledge_regressions"
                " WHERE kb_id = %s AND tenant_id = %s",
                (_key(kb_id), DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            return Regression(**row) if row else None

    async def save_regression(
        self,
        kb_id: str,
        cases: list[dict[str, Any]],
        retrieval: dict[str, Any] | None,
        top_k: int,
        min_score: float,
        updated_by: str | None,
    ) -> Regression:
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "INSERT INTO knowledge_regressions"
                    " (kb_id, cases, retrieval, top_k, min_score, updated_by, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, now()) ON CONFLICT (kb_id) DO UPDATE"
                    " SET cases = excluded.cases, retrieval = excluded.retrieval,"
                    " top_k = excluded.top_k, min_score = excluded.min_score,"
                    " updated_by = excluded.updated_by, updated_at = excluded.updated_at"
                    f" RETURNING {REGRESSION_COLUMNS}",
                    (
                        _key(kb_id),
                        Jsonb(cases),
                        Jsonb(retrieval) if retrieval is not None else None,
                        top_k,
                        min_score,
                        updated_by,
                    ),
                )
            except errors.ForeignKeyViolation as exc:
                raise MissingBase(kb_id) from exc
            return Regression(**await cur.fetchone())

    async def save_regression_run(self, kb_id: str, last_run: dict[str, Any]) -> Regression:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE knowledge_regressions SET last_run = %s"
                f" WHERE kb_id = %s AND tenant_id = %s RETURNING {REGRESSION_COLUMNS}",
                (Jsonb(last_run), _key(kb_id), DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            if row is None:
                raise MissingBase(kb_id)
            return Regression(**row)

    async def get_document(self, kb_id: str, document_id: str) -> Document | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {DOCUMENT_COLUMNS} FROM knowledge_documents"
                " WHERE kb_id = %s AND document_id = %s AND tenant_id = %s",
                (_key(kb_id), _key(document_id), DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            return Document(**row) if row else None

    async def delete_document(self, kb_id: str, document_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "DELETE FROM knowledge_documents"
                " WHERE kb_id = %s AND document_id = %s AND tenant_id = %s RETURNING 1",
                (_key(kb_id), _key(document_id), DEFAULT_TENANT),
            )
            if await cur.fetchone():
                await self._changed(conn, kb_id)

    async def chunks(self, document_id: str, offset: int, limit: int) -> tuple[int, list[Chunk]]:
        async with self._pool.connection() as conn, conn.transaction():
            # One statement gives a consistent count even if reindexing replaces the chunks.
            cur = await conn.execute(
                "WITH selected AS (SELECT * FROM knowledge_chunks"
                " WHERE document_id = %s AND tenant_id = %s)"
                " SELECT (SELECT count(*) FROM selected) AS total,"
                " coalesce((SELECT jsonb_agg(chunk_page) FROM"
                f" (SELECT {CHUNK_COLUMNS} FROM selected ORDER BY position"
                " OFFSET %s LIMIT %s) chunk_page), '[]'::jsonb) AS items",
                (_key(document_id), DEFAULT_TENANT, offset, limit),
            )
            row = await cur.fetchone()
            return row["total"], [Chunk(**item) for item in row["items"]]

    async def requeue(self, kb_id: str, document_id: str | None = None) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE knowledge_documents d SET status = 'queued', error = NULL,"
                " chunk_count = 0, updated_at = now(),"
                " chunk_size = b.chunk_size, chunk_overlap = b.chunk_overlap"
                " FROM knowledge_bases b WHERE d.kb_id = b.kb_id"
                " AND d.kb_id = %s AND d.tenant_id = %s"
                + (" AND d.document_id = %s" if document_id is not None else "")
                + " RETURNING d.document_id",
                (_key(kb_id), DEFAULT_TENANT)
                + ((_key(document_id),) if document_id is not None else ()),
            )
            ids = [row["document_id"] for row in await cur.fetchall()]
            await conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ANY(%s)", (ids,))
            return len(ids)

    async def claim(self) -> Document | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT document_id FROM knowledge_documents"
                " WHERE status = 'queued' AND tenant_id = %s"
                " ORDER BY created_at, document_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (DEFAULT_TENANT,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            cur = await conn.execute(
                "UPDATE knowledge_documents SET status = 'processing', error = NULL,"
                f" updated_at = now() WHERE document_id = %s RETURNING {DOCUMENT_COLUMNS}",
                (row["document_id"],),
            )
            return Document(**await cur.fetchone())

    async def recover_stale(self, before: datetime) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE knowledge_documents SET status = 'queued', error = NULL,"
                " chunk_count = 0, updated_at = now()"
                " WHERE status = 'processing' AND updated_at < %s AND tenant_id = %s"
                " RETURNING document_id",
                (before, DEFAULT_TENANT),
            )
            ids = [row["document_id"] for row in await cur.fetchall()]
            await conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ANY(%s)", (ids,))
            return len(ids)

    async def touch(self, document: Document) -> Document | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE knowledge_documents SET updated_at = now()"
                " WHERE document_id = %s AND status = 'processing' AND updated_at = %s"
                f" AND tenant_id = %s RETURNING {DOCUMENT_COLUMNS}",
                (_key(document.document_id), document.updated_at, DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            return Document(**row) if row else None

    async def _lock_claim(self, conn: AsyncConnection, document: Document) -> bool:
        cur = await conn.execute(
            "SELECT 1 FROM knowledge_documents WHERE document_id = %s"
            " AND status = 'processing' AND updated_at = %s AND tenant_id = %s FOR UPDATE",
            (_key(document.document_id), document.updated_at, DEFAULT_TENANT),
        )
        return await cur.fetchone() is not None

    async def backfill_keywords(self, limit: int = 500) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT chunk_id, text, heading FROM knowledge_chunks"
                " WHERE keywords IS NULL AND tenant_id = %s ORDER BY chunk_id"
                " LIMIT %s FOR UPDATE SKIP LOCKED",
                (DEFAULT_TENANT, limit),
            )
            rows = await cur.fetchall()
            values = await asyncio.to_thread(
                lambda: [(chunk_keywords(r["text"], r["heading"]), r["chunk_id"]) for r in rows]
            )
            async with conn.cursor() as cur:
                await cur.executemany(
                    "UPDATE knowledge_chunks SET keywords = %s WHERE chunk_id = %s", values
                )
            return len(rows)

    async def replace_chunks(self, document: Document, chunks: list[Chunk]) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            if not await self._lock_claim(conn, document):
                return
            await conn.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = %s", (_key(document.document_id),)
            )
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO knowledge_chunks (chunk_id, document_id, kb_id, position,"
                    " text, heading, page, embedding, keywords)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
                    [
                        (
                            c.chunk_id,
                            document.document_id,
                            document.kb_id,
                            c.position,
                            c.text,
                            c.heading,
                            c.page,
                            _vector(c.embedding),
                            c.keywords,
                        )
                        for c in chunks
                    ],
                )
            await conn.execute(
                "UPDATE knowledge_documents SET status = 'ready', error = NULL,"
                " chunk_count = %s, updated_at = now() WHERE document_id = %s",
                (len(chunks), _key(document.document_id)),
            )

    async def fail(self, document: Document, error: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            if not await self._lock_claim(conn, document):
                return
            await conn.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = %s", (_key(document.document_id),)
            )
            await conn.execute(
                "UPDATE knowledge_documents SET status = 'error', error = %s,"
                " chunk_count = 0, updated_at = now() WHERE document_id = %s",
                (error, _key(document.document_id)),
            )

    async def vector_candidates(
        self, kb_id: str, vector: list[float], n: int, *, include_inactive: bool = False
    ) -> list[str]:
        query = _vector(vector)
        async with self._pool.connection() as conn, conn.transaction():
            # HNSW filters after scanning: continue until this base has n ready rows. Order by
            # distance alone: a tie-breaker column would stop the planner using the index.
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            cur = await conn.execute(
                "SELECT c.chunk_id::text FROM knowledge_chunks c"
                " JOIN knowledge_documents d USING (document_id)"
                " JOIN knowledge_bases b ON b.kb_id = c.kb_id"
                " WHERE c.kb_id = %(kb)s AND b.tenant_id = %(tenant)s AND d.status = 'ready'"
                + self._active(include_inactive)
                + " ORDER BY c.embedding <=> %(query)s::vector LIMIT %(n)s",
                {
                    "kb": _key(kb_id),
                    "tenant": DEFAULT_TENANT,
                    "today": self._today(),
                    "query": query,
                    "n": n,
                },
            )
            return [row["chunk_id"] for row in await cur.fetchall()]

    async def keyword_candidates(
        self, kb_id: str, terms: list[str], n: int, *, include_inactive: bool = False
    ) -> list[str]:
        terms = _query_terms(terms)
        if not terms:
            return []
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT c.chunk_id::text, " + KEYWORD_SCORE + " AS score"
                " FROM knowledge_chunks c JOIN knowledge_documents d USING (document_id)"
                " JOIN knowledge_bases b ON b.kb_id = c.kb_id"
                " WHERE c.kb_id = %(kb)s AND b.tenant_id = %(tenant)s AND d.status = 'ready'"
                + self._active(include_inactive)
                + " AND c.tsv @@ websearch_to_tsquery('simple', %(query)s)"
                " ORDER BY score DESC, c.chunk_id LIMIT %(n)s",
                {
                    "words": terms,
                    "kb": _key(kb_id),
                    "tenant": DEFAULT_TENANT,
                    "today": self._today(),
                    "query": " or ".join(terms),
                    "n": n,
                },
            )
            return [row["chunk_id"] for row in await cur.fetchall()]

    async def score_chunks(
        self,
        kb_id: str,
        chunk_ids: list[str],
        vector: list[float] | None,
        terms: list[str] | None,
        *,
        include_inactive: bool = False,
    ) -> list[Hit]:
        if not chunk_ids:
            return []
        query = _vector(vector) if vector is not None else None
        words = list(dict.fromkeys(terms or []))
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT b.kb_id::text, b.name AS kb_name, d.document_id::text,"
                " d.name AS document_name, c.chunk_id::text, c.position, c.text, c.heading, c.page,"
                " CASE WHEN %(query)s::vector IS NULL THEN NULL"
                " WHEN (c.embedding <=> %(query)s::vector) = 'NaN'::float8 THEN 0.0"
                " ELSE greatest(0.0, least(1.0, 1 - (c.embedding <=> %(query)s::vector)))"
                " END AS vector,"
                " CASE WHEN %(keyword)s::boolean THEN " + KEYWORD_SCORE + " ELSE NULL END"
                " AS keyword"
                " FROM knowledge_chunks c JOIN knowledge_documents d USING (document_id)"
                " JOIN knowledge_bases b ON b.kb_id = c.kb_id"
                " WHERE c.kb_id = %(kb)s AND b.tenant_id = %(tenant)s AND d.status = 'ready'"
                + self._active(include_inactive)
                + " AND c.chunk_id = ANY(%(ids)s::uuid[])",
                {
                    "query": query,
                    "keyword": terms is not None,
                    "words": words,
                    "kb": _key(kb_id),
                    "tenant": DEFAULT_TENANT,
                    "today": self._today(),
                    "ids": [_key(key) for key in chunk_ids],
                },
            )
            hits = []
            for row in await cur.fetchall():
                scores = {
                    "vector": row.pop("vector"),
                    "keyword": row.pop("keyword"),
                    "rerank": None,
                }
                score = (
                    scores["vector"] if scores["vector"] is not None else scores["keyword"] or 0.0
                )
                hits.append(Hit(**row, score=score, scores=scores))
            return hits

    async def search(
        self, kb_id: str, vector: list[float], top_k: int, min_score: float
    ) -> list[Hit]:
        ids = await self.vector_candidates(kb_id, vector, top_k)
        hits = [
            h for h in await self.score_chunks(kb_id, ids, vector, None) if h.score >= min_score
        ]
        return sorted(hits, key=lambda h: (-h.score, h.chunk_id))[:top_k]
