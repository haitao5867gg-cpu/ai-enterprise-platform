"""Knowledge records with interchangeable in-memory and Postgres stores."""

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from psycopg import AsyncConnection, errors
from psycopg_pool import AsyncConnectionPool

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
    async def add_documents(self, kb_id: str, documents: list[Document]) -> list[Document]: ...
    async def get_document(self, kb_id: str, document_id: str) -> Document | None: ...
    async def delete_document(self, kb_id: str, document_id: str) -> None: ...
    async def chunks(
        self, document_id: str, offset: int, limit: int
    ) -> tuple[int, list[Chunk]]: ...
    async def requeue(self, kb_id: str, document_id: str | None = None) -> int: ...
    async def claim(self) -> Document | None: ...
    async def recover_stale(self, before: datetime) -> int: ...
    async def touch(self, document: Document) -> Document | None: ...
    async def replace_chunks(self, document: Document, chunks: list[Chunk]) -> None: ...
    async def fail(self, document: Document, error: str) -> None: ...
    async def search(
        self, kb_id: str, vector: list[float], top_k: int, min_score: float
    ) -> list[Hit]: ...


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

    def __init__(self) -> None:
        self._bases: dict[str, KnowledgeBase] = {}
        self._documents: dict[str, Document] = {}
        self._chunks: dict[str, list[Chunk]] = {}

    async def setup(self) -> None:
        return None

    def _view(self, base: KnowledgeBase) -> KnowledgeBase:
        documents = [d for d in self._documents.values() if d.kb_id == base.kb_id]
        return replace(
            base,
            document_count=len(documents),
            chunk_count=sum(d.chunk_count for d in documents),
            status_counts={
                status: sum(d.status == status for d in documents) for status in STATUSES
            },
        )

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
        for key, document in list(self._documents.items()):
            if document.kb_id == kb_id:
                self._documents.pop(key)
                self._chunks.pop(key, None)

    async def documents(self, kb_id: str) -> list[Document]:
        return sorted(
            [d for d in self._documents.values() if d.kb_id == kb_id],
            key=lambda d: (d.created_at, d.document_id),
            reverse=True,
        )

    async def add_documents(self, kb_id: str, documents: list[Document]) -> list[Document]:
        if kb_id not in self._bases:
            raise MissingBase(kb_id)
        taken = {d.name for d in self._documents.values() if d.kb_id == kb_id}
        if len(taken) + len(documents) > MAX_DOCUMENTS:
            raise DocumentLimit("一个知识库最多 500 个文档")
        base = self._bases[kb_id]
        added = [
            replace(
                d,
                name=_unique_filename(d.name, taken),
                chunk_size=base.chunk_size,
                chunk_overlap=base.chunk_overlap,
            )
            for d in documents
        ]
        self._documents.update((d.document_id, d) for d in added)
        return added

    async def get_document(self, kb_id: str, document_id: str) -> Document | None:
        document = self._documents.get(document_id)
        return document if document and document.kb_id == kb_id else None

    async def delete_document(self, kb_id: str, document_id: str) -> None:
        if await self.get_document(kb_id, document_id):
            self._documents.pop(document_id)
            self._chunks.pop(document_id, None)

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

    async def search(
        self, kb_id: str, vector: list[float], top_k: int, min_score: float
    ) -> list[Hit]:
        base = self._bases.get(kb_id)
        if base is None:
            return []
        norm = math.sqrt(sum(v * v for v in vector))
        hits: list[Hit] = []
        for document in self._documents.values():
            if document.kb_id != kb_id or document.status != "ready":
                continue
            for chunk in self._chunks.get(document.document_id, []):
                other_norm = math.sqrt(sum(v * v for v in chunk.embedding))
                score = (
                    sum(a * b for a, b in zip(vector, chunk.embedding, strict=True))
                    / (norm * other_norm)
                    if norm and other_norm
                    else 0.0
                )
                score = max(0.0, min(1.0, score))
                if score >= min_score:
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
                            score,
                        )
                    )
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
       ) AS status_counts
FROM knowledge_bases b LEFT JOIN knowledge_documents d ON b.kb_id = d.kb_id
WHERE b.tenant_id = %s
"""
DOCUMENT_COLUMNS = (
    "document_id::text, kb_id::text, tenant_id, name, kind, size_bytes, path, status,"
    " error, chunk_count, created_at, updated_at, chunk_size, chunk_overlap"
)
CHUNK_COLUMNS = (
    "chunk_id::text, document_id::text, kb_id::text, tenant_id, position, text, heading, page"
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

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def _get(self, conn: AsyncConnection, kb_id: str) -> KnowledgeBase | None:
        cur = await conn.execute(
            BASE_SELECT + " AND b.kb_id = %s GROUP BY b.kb_id", (DEFAULT_TENANT, _key(kb_id))
        )
        row = await cur.fetchone()
        return KnowledgeBase(**row) if row else None

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
                BASE_SELECT + " GROUP BY b.kb_id ORDER BY b.name", (DEFAULT_TENANT,)
            )
            return [KnowledgeBase(**row) for row in await cur.fetchall()]

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

    async def add_documents(self, kb_id: str, documents: list[Document]) -> list[Document]:
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
                "SELECT name FROM knowledge_documents WHERE kb_id = %s", (_key(kb_id),)
            )
            taken = {row["name"] for row in await cur.fetchall()}
            if len(taken) + len(documents) > MAX_DOCUMENTS:
                raise DocumentLimit("一个知识库最多 500 个文档")
            added: list[Document] = []
            for document in documents:
                name = _unique_filename(document.name, taken)
                cur = await conn.execute(
                    "INSERT INTO knowledge_documents"
                    " (document_id, kb_id, name, kind, size_bytes, path, chunk_size, chunk_overlap)"
                    f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {DOCUMENT_COLUMNS}",
                    (
                        document.document_id,
                        kb_id,
                        name,
                        document.kind,
                        document.size_bytes,
                        document.path,
                        base["chunk_size"],
                        base["chunk_overlap"],
                    ),
                )
                added.append(Document(**await cur.fetchone()))
            return added

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
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM knowledge_documents"
                " WHERE kb_id = %s AND document_id = %s AND tenant_id = %s",
                (_key(kb_id), _key(document_id), DEFAULT_TENANT),
            )

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
                    " text, heading, page, embedding)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
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

    async def search(
        self, kb_id: str, vector: list[float], top_k: int, min_score: float
    ) -> list[Hit]:
        query = _vector(vector)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM ("
                " SELECT b.kb_id::text, b.name AS kb_name, d.document_id::text,"
                " d.name AS document_name, c.chunk_id::text, c.position, c.text, c.heading, c.page,"
                " CASE WHEN (c.embedding <=> %s::vector) = 'NaN'::float8 THEN 0.0"
                " ELSE greatest(0.0, least(1.0, 1 - (c.embedding <=> %s::vector))) END AS score"
                " FROM knowledge_chunks c JOIN knowledge_documents d USING (document_id)"
                " JOIN knowledge_bases b ON b.kb_id = c.kb_id"
                " WHERE c.kb_id = %s AND b.tenant_id = %s AND d.status = 'ready'"
                " ORDER BY c.embedding <=> %s::vector LIMIT %s"
                ") hits WHERE score >= %s ORDER BY score DESC, chunk_id",
                (query, query, _key(kb_id), DEFAULT_TENANT, query, top_k, min_score),
            )
            return [Hit(**row) for row in await cur.fetchall()]
