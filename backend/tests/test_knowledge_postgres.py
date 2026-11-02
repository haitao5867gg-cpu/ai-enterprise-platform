"""pgvector integration tests, enabled by AEP_TEST_DATABASE_URL (as in CI)."""

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.knowledge.store import (
    Chunk,
    Document,
    DuplicateName,
    KnowledgeBase,
    ModelInUse,
    PostgresKnowledgeStore,
)

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@dataclass
class Database:
    store: PostgresKnowledgeStore
    pool: AsyncConnectionPool
    base: KnowledgeBase

    async def documents(self, count: int = 1) -> list[Document]:
        return await self.store.add_documents(
            self.base.kb_id,
            [
                Document(str(uuid4()), self.base.kb_id, f"{i}.txt", "txt", 100, f"/unused/{i}.txt")
                for i in range(count)
            ],
        )


@pytest.fixture
async def db():
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    store = PostgresKnowledgeStore(pool)
    await store.setup()
    base = await store.create(f"knowledge-test-{uuid4().hex}")
    try:
        yield Database(store, pool, base)
    finally:
        await store.delete(base.kb_id)
        await pool.close()


def unit(index: int, value: float = 1) -> list[float]:
    vector = [0.0] * 1024
    vector[index] = value
    return vector


async def test_setup_crud_stats_and_model_conflict(db):
    await db.store.setup()  # Idempotent schema and HNSW index creation.
    assert await db.store.get(db.base.kb_id) == db.base
    assert db.base.kb_id in [b.kb_id for b in await db.store.all()]
    assert await db.store.get("bad-uuid") is None
    with pytest.raises(DuplicateName):
        await db.store.create(db.base.name)
    updated = await db.store.update(
        db.base.kb_id, description="说明", chunk_size=200, chunk_overlap=20
    )
    assert updated.description == "说明" and updated.chunk_size == 200
    documents = await db.documents(2)
    [renamed] = await db.store.add_documents(
        db.base.kb_id, [Document(str(uuid4()), db.base.kb_id, "0.txt", "txt", 0, "/unused/0.txt")]
    )
    assert renamed.name == "0 (2).txt"
    assert (await db.store.documents(db.base.kb_id))[0] == renamed
    assert await db.store.get_document(db.base.kb_id, documents[0].document_id) == documents[0]
    with pytest.raises(ModelInUse):
        await db.store.update(db.base.kb_id, embedding_model="company-embed")
    stats = await db.store.get(db.base.kb_id)
    assert stats.document_count == stats.status_counts["queued"] == 3


async def test_transactional_chunks_vector_search_pagination_and_cascade(db):
    [document] = await db.documents()
    claimed = await db.store.claim()
    assert claimed.document_id == document.document_id
    touched = await db.store.touch(claimed)
    assert touched.updated_at > claimed.updated_at
    chunks = [
        Chunk(
            str(uuid4()),
            document.document_id,
            db.base.kb_id,
            0,
            "退货期限",
            "政策 > 期限",
            2,
            unit(0),
        ),
        Chunk(
            str(uuid4()), document.document_id, db.base.kb_id, 1, "数据库连接", embedding=unit(1)
        ),
        Chunk(
            str(uuid4()), document.document_id, db.base.kb_id, 2, "负相似度", embedding=unit(0, -1)
        ),
    ]
    await db.store.replace_chunks(touched, chunks)
    ready = await db.store.get_document(db.base.kb_id, document.document_id)
    assert ready.status == "ready" and ready.chunk_count == 3
    total, items = await db.store.chunks(document.document_id, 1, 1)
    assert total == 3 and items[0].chunk_id == chunks[1].chunk_id
    assert await db.store.chunks(document.document_id, 99, 1) == (3, [])
    hits = await db.store.search(db.base.kb_id, unit(0), 1, 0)
    assert len(hits) == 1 and hits[0].chunk_id == chunks[0].chunk_id
    assert hits[0].score == pytest.approx(1) and hits[0].page == 2
    assert hits[0].heading == "政策 > 期限"
    assert len(await db.store.search(db.base.kb_id, unit(0), 20, 0.5)) == 1
    assert [h.score for h in await db.store.search(db.base.kb_id, unit(0), 20, 0)] == [1, 0, 0]
    assert all(h.score == 0 for h in await db.store.search(db.base.kb_id, [0.0] * 1024, 20, 0))
    # Even persisted chunks are excluded while their document is not ready.
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE knowledge_documents SET status = 'processing' WHERE document_id = %s",
            (document.document_id,),
        )
    assert await db.store.search(db.base.kb_id, unit(0), 20, 0) == []
    await db.store.delete_document(db.base.kb_id, document.document_id)
    assert await db.store.chunks(document.document_id, 0, 10) == (0, [])
    assert (await db.store.get(db.base.kb_id)).chunk_count == 0


async def test_concurrent_claims_and_skip_locked_rows(db):
    documents = await db.documents(3)
    async with db.pool.connection() as conn, conn.transaction():
        await conn.execute(
            "SELECT 1 FROM knowledge_documents WHERE document_id = %s FOR UPDATE",
            (documents[0].document_id,),
        )
        first, second = await asyncio.wait_for(
            asyncio.gather(db.store.claim(), db.store.claim()), 2
        )
        assert {first.document_id, second.document_id} == {d.document_id for d in documents[1:]}
    third = await db.store.claim()
    assert third.document_id == documents[0].document_id
    assert await db.store.claim() is None


async def test_recovery_reindex_failure_and_old_claim_guards(db):
    [document] = await db.documents()
    claimed = await db.store.claim()
    assert await db.store.recover_stale(datetime.now(UTC) - timedelta(minutes=10)) == 0
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE knowledge_documents SET updated_at = %s WHERE document_id = %s",
            (datetime.now(UTC) - timedelta(minutes=11), document.document_id),
        )
    assert await db.store.recover_stale(datetime.now(UTC) - timedelta(minutes=10)) == 1
    current = await db.store.claim()
    assert await db.store.touch(claimed) is None
    await db.store.fail(claimed, "过期任务失败")
    assert await db.store.get_document(db.base.kb_id, document.document_id) == current
    chunk = Chunk(str(uuid4()), document.document_id, db.base.kb_id, 0, "正文", embedding=unit(0))
    await db.store.replace_chunks(current, [chunk])
    assert await db.store.requeue(db.base.kb_id, document.document_id) == 1
    assert await db.store.chunks(document.document_id, 0, 10) == (0, [])
    next_claim = await db.store.claim()
    await db.store.fail(next_claim, "网关 500")
    failed = await db.store.get_document(db.base.kb_id, document.document_id)
    assert failed.status == "error" and failed.error == "网关 500" and failed.chunk_count == 0
    assert await db.store.requeue(db.base.kb_id) == 1
    current = await db.store.claim()
    # Invalid dimensions roll back the whole replacement, including its delete.
    with pytest.raises(errors.DataException):
        await db.store.replace_chunks(
            current,
            [
                chunk,
                Chunk(
                    str(uuid4()),
                    document.document_id,
                    db.base.kb_id,
                    1,
                    "错误向量",
                    embedding=[1.0],
                ),
            ],
        )
    assert await db.store.chunks(document.document_id, 0, 10) == (0, [])
    await db.store.replace_chunks(current, [chunk])
    await db.store.delete(db.base.kb_id)
    assert await db.store.get_document(db.base.kb_id, document.document_id) is None
    assert await db.store.chunks(document.document_id, 0, 10) == (0, [])


async def test_chunking_snapshots_are_durable_across_claim_and_reindex(db):
    [document] = await db.documents()
    await db.store.update(db.base.kb_id, chunk_size=200, chunk_overlap=20)
    # Recreate the store to exercise persisted snapshots rather than process state.
    restarted = PostgresKnowledgeStore(db.pool)
    claimed = await restarted.claim()
    assert claimed.chunk_size == document.chunk_size == 800
    assert claimed.chunk_overlap == document.chunk_overlap == 100
    await restarted.requeue(db.base.kb_id, document.document_id)
    current = await restarted.claim()
    assert current.chunk_size == 200 and current.chunk_overlap == 20


async def test_governance_dates_versions_hits_and_regression(db):
    from aep.knowledge.store import ReplaceKind, today_in

    kb = db.base.kb_id
    [document] = await db.documents()
    assert document.version == 1 and document.owner is None and document.uploaded_by is None
    claimed = await db.store.claim()
    await db.store.replace_chunks(
        claimed,
        [
            Chunk(
                str(uuid4()),
                document.document_id,
                kb,
                0,
                "退货期限",
                embedding=unit(0),
                keywords="退货 期限",
            )
        ],
    )
    today = today_in(db.store.timezone)
    assert len(await db.store.vector_candidates(kb, unit(0), 5)) == 1

    # Expired documents drop out of every candidate query unless asked for.
    updated = await db.store.update_document(
        kb, document.document_id, owner="张三", valid_until=today - timedelta(days=1)
    )
    assert updated.owner == "张三" and updated.validity(today) == "expired"
    base = await db.store.get(kb)
    assert base.validity_counts == {"active": 0, "pending": 0, "expired": 1}
    assert base.docs_changed_at is not None
    assert await db.store.vector_candidates(kb, unit(0), 5) == []
    assert await db.store.keyword_candidates(kb, ["退货"], 5) == []
    ids = await db.store.vector_candidates(kb, unit(0), 5, include_inactive=True)
    assert len(ids) == 1
    assert await db.store.score_chunks(kb, ids, unit(0), ["退货"]) == []
    assert len(await db.store.score_chunks(kb, ids, unit(0), ["退货"], include_inactive=True)) == 1
    await db.store.update_document(
        kb, document.document_id, valid_from=today + timedelta(days=1), valid_until=None
    )
    assert (await db.store.get(kb)).validity_counts["pending"] == 1
    assert await db.store.keyword_candidates(kb, ["退货"], 5) == []
    await db.store.update_document(kb, document.document_id, valid_from=None)
    assert len(await db.store.keyword_candidates(kb, ["退货"], 5)) == 1

    # Replacing keeps the id, bumps the version and queues it again without chunks.
    new = Document(str(uuid4()), kb, document.name, "txt", 7, "/unused/new.txt")
    [replaced] = await db.store.add_documents(kb, [new], replace=True, uploaded_by="李四")
    assert replaced.document_id == document.document_id and replaced.version == 2
    assert replaced.status == "queued" and replaced.path == "/unused/new.txt"
    assert replaced.owner == "张三" and replaced.uploaded_by == "李四"
    assert await db.store.chunks(document.document_id, 0, 10) == (0, [])
    versions = await db.store.versions(document.document_id)
    assert [(v.version, v.size_bytes, v.uploaded_by) for v in versions] == [
        (2, 7, "李四"),
        (1, 100, None),
    ]
    with pytest.raises(ReplaceKind):
        await db.store.add_documents(
            kb, [Document(str(uuid4()), kb, document.name, "md", 1, "/x")], replace=True
        )

    # Hits count once per run and document; deleted documents are skipped.
    since = datetime.now(UTC) - timedelta(days=30)
    pairs = [(kb, document.document_id)] * 2
    await db.store.record_hits("run-1", pairs)
    await db.store.record_hits("run-2", pairs + [(kb, str(uuid4()))])
    stats = await db.store.hit_stats(kb, since)
    assert stats[document.document_id].hits_30d == 2
    assert stats[document.document_id].last_hit_at is not None

    assert await db.store.get_regression(kb) is None
    case = {"id": "c1", "query": "退货", "expected": [document.name]}
    saved = await db.store.save_regression(kb, [case], {"mode": "keyword"}, 3, 0.1, "王五")
    assert saved.cases == [case] and saved.top_k == 3 and saved.updated_by == "王五"
    run = {"at": datetime.now(UTC).isoformat(), "passed": 1, "total": 1, "score": 1.0}
    assert (await db.store.save_regression_run(kb, run)).last_run == run
    again = await db.store.save_regression(kb, [], None, 5, 0.3, None)
    assert again.last_run == run and again.retrieval is None
    await db.store.delete_document(kb, document.document_id)
    assert await db.store.hit_stats(kb, since) == {}
