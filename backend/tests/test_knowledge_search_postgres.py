"""Real pgvector/GIN retrieval and upgrades; opt in with AEP_TEST_DATABASE_URL."""

import json
import os
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.knowledge.search import search
from aep.knowledge.store import SCHEMA, PostgresKnowledgeStore
from tests.test_knowledge_search import assert_hybrid_union, seed, vector

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@pytest.fixture
async def pg_pool():
    schema = f"knowledge75_{uuid4().hex}"
    async with await AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        pool = AsyncConnectionPool(
            DATABASE_URL,
            open=False,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "options": f"-c search_path={schema},public",
            },
        )
        try:
            await pool.open(wait=True)
            yield pool
        finally:
            await pool.close()
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
async def pg_store(pg_pool):
    store = PostgresKnowledgeStore(pg_pool)
    await store.setup()
    return store


async def test_old_schema_upgrade_backfill_and_keyword_retrieval(pg_pool, gateway):
    # Build the first release's actual table, and insert data before setup upgrades it.
    async with pg_pool.connection() as conn:
        for statement in SCHEMA[:3]:
            await conn.execute(statement)
        await conn.execute("""
            CREATE TABLE knowledge_chunks (
                chunk_id uuid PRIMARY KEY,
                document_id uuid NOT NULL REFERENCES knowledge_documents(document_id)
                    ON DELETE CASCADE,
                kb_id uuid NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                tenant_id text NOT NULL DEFAULT 'default',
                position integer NOT NULL, text text NOT NULL,
                heading text NOT NULL DEFAULT '', page integer,
                embedding vector(1024) NOT NULL, UNIQUE (document_id, position)
            )
        """)
        kb_id, document_id, chunk_id = [str(uuid4()) for _ in range(3)]
        await conn.execute(
            "INSERT INTO knowledge_bases (kb_id, name, embedding_model, chunk_size, chunk_overlap)"
            " VALUES (%s, '旧资料', 'mock-embed', 800, 100)",
            (kb_id,),
        )
        await conn.execute(
            "INSERT INTO knowledge_documents"
            " (document_id, kb_id, name, kind, size_bytes, path, status,"
            " chunk_size, chunk_overlap, chunk_count)"
            " VALUES (%s, %s, '政策.md', 'md', 100, '/unused.md', 'ready', 800, 100, 1)",
            (document_id, kb_id),
        )
        from aep import llm

        [embedding] = await llm.embed(gateway.client, ["退货期限"], "mock-embed")
        await conn.execute(
            "INSERT INTO knowledge_chunks"
            " (chunk_id, document_id, kb_id, position, text, heading, embedding)"
            " VALUES (%s, %s, %s, 0, '三十天内申请', '退货期限', %s::vector)",
            (chunk_id, document_id, kb_id, json.dumps(embedding)),
        )
    store = PostgresKnowledgeStore(pg_pool)
    await store.setup()
    await store.setup()
    [before] = await search(store, gateway.client, [kb_id], "退货期限", min_score=0)
    assert before.score == pytest.approx(1)
    assert await store.keyword_candidates(kb_id, ["退货", "期限"], 20) == []
    assert await store.backfill_keywords() == 1
    assert await store.backfill_keywords() == 0
    assert await search(store, gateway.client, [kb_id], "退货期限", min_score=0) == [before]
    [hit] = await search(store, gateway.client, [kb_id], "退货期限", retrieval={"mode": "keyword"})
    assert hit.chunk_id == chunk_id and hit.score == 1
    assert hit.scores == {"vector": None, "keyword": 1, "rerank": None}
    async with pg_pool.connection() as conn:
        cur = await conn.execute("SELECT keywords, embedding::text FROM knowledge_chunks")
        row = await cur.fetchone()
        assert row["keywords"].startswith("退货 期限")
        assert json.loads(row["embedding"]) == pytest.approx(embedding)
        cur = await conn.execute(
            "SELECT is_generated FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'knowledge_chunks'"
            " AND column_name = 'tsv'"
        )
        assert (await cur.fetchone())["is_generated"] == "ALWAYS"
        cur = await conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
        )
        assert any("USING gin (tsv)" in row["indexdef"] for row in await cur.fetchall())


async def test_hybrid_union_computes_real_cosine_for_keyword_only_candidate(
    pg_store, gateway, monkeypatch
):
    await assert_hybrid_union(pg_store, gateway, monkeypatch)


async def test_keyword_scoring_handles_special_characters_without_tsquery_injection(pg_store):
    base = await pg_store.create("特殊字符")
    words = ["a'b", r"back\slash", "colon:term", "rock&roll", "pipe|term", "bang!term", '"quoted"']
    [chunk] = await seed(
        pg_store, base, [{"text": "特殊关键词", "embedding": vector(), "keywords": " ".join(words)}]
    )
    for word in words:
        # websearch safely handles punctuation/quotes; scoring compares exact tokens.
        assert await pg_store.keyword_candidates(base.kb_id, [word, "or"], 20) == [chunk.chunk_id]
        [hit] = await pg_store.score_chunks(
            base.kb_id, [chunk.chunk_id], None, [word, "absent", word]
        )
        assert hit.score == hit.scores["keyword"] == 0.5
    assert await pg_store.keyword_candidates(base.kb_id, ["or"], 20) == []
    assert (
        await pg_store.keyword_candidates(base.kb_id, ["'", '"', "\\", ":", "&", "|", "!"], 20)
        == []
    )


async def test_keyword_ranking_ready_filter_base_isolation_and_zero_vector(pg_store, pg_pool):
    base, other = await pg_store.create("资料"), await pg_store.create("别的库")
    chunks = await seed(
        pg_store,
        base,
        [
            {"text": "完整", "embedding": vector(), "keywords": "退货 期限"},
            {"text": "部分", "embedding": vector(0), "keywords": "退货"},
            {"text": "无词", "embedding": vector(-1), "keywords": None},
        ],
    )
    other_chunks = await seed(
        pg_store, other, [{"text": "另一个库", "embedding": vector(), "keywords": "退货 期限"}]
    )
    assert await pg_store.keyword_candidates(base.kb_id, ["退货", "期限"], 20) == [
        chunks[0].chunk_id,
        chunks[1].chunk_id,
    ]
    scored = await pg_store.score_chunks(
        base.kb_id, [c.chunk_id for c in chunks + other_chunks], vector(), ["退货", "期限"]
    )
    by_id = {h.chunk_id: h for h in scored}
    assert by_id[chunks[0].chunk_id].scores == {"vector": 1, "keyword": 1, "rerank": None}
    assert by_id[chunks[1].chunk_id].scores == {"vector": 0, "keyword": 0.5, "rerank": None}
    assert by_id[chunks[2].chunk_id].scores == {"vector": 0, "keyword": 0, "rerank": None}
    for status in ["queued", "processing", "error"]:
        async with pg_pool.connection() as conn:
            await conn.execute(
                "UPDATE knowledge_documents SET status = %s WHERE document_id = %s",
                (status, chunks[0].document_id),
            )
        assert await pg_store.keyword_candidates(base.kb_id, ["退货"], 20) == []
        assert await pg_store.vector_candidates(base.kb_id, vector(), 20) == []
        assert (
            await pg_store.score_chunks(
                base.kb_id, [c.chunk_id for c in chunks], vector(), ["退货"]
            )
            == []
        )
