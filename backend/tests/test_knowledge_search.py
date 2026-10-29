"""Hybrid retrieval acceptance tests, using local vectors and in-process models."""

import asyncio
import math
import threading
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aep import llm
from aep.knowledge import keywords
from aep.knowledge.search import search
from aep.knowledge.store import Chunk, Document, MemoryKnowledgeStore, MissingBase
from aep.knowledge.worker import KnowledgeWorker


def vector(cosine: float = 1) -> list[float]:
    return [cosine, math.sqrt(1 - cosine * cosine)] + [0.0] * 1022


async def seed(store, base, rows: list[dict]) -> list[Chunk]:
    document = Document(str(uuid4()), base.kb_id, f"{uuid4()}.md", "md", 100, "/unused.md")
    await store.add_documents(base.kb_id, [document])
    claim = await store.claim()
    assert claim.document_id == document.document_id
    chunks = [
        Chunk(str(uuid4()), document.document_id, base.kb_id, i, **row)
        for i, row in enumerate(rows)
    ]
    await store.replace_chunks(claim, chunks)
    return chunks


def test_terms_normalize_filter_stopwords_and_preserve_search_mode_order():
    assert keywords.terms("ＴＨＥ 数据库连接，退货期限 ABC １２３！和的了") == [
        "数据",
        "据库",
        "数据库",
        "连接",
        "退货",
        "期限",
        "abc",
        "123",
    ]
    assert keywords.terms("退货 退货") == ["退货", "退货"]
    assert keywords.terms('the AND or 和 的 了！ & | ! \\"') == []


async def test_keyword_mode_works_with_mock_embeddings_and_never_calls_embed(gateway, monkeypatch):
    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    [policy, database] = await seed(
        store,
        base,
        [
            {"text": "商品退货的申请期限为三十天", "embedding": vector(), "keywords": "退货 期限"},
            {
                "text": "数据库连接配置方法",
                "embedding": vector(),
                "keywords": "数据 据库 数据库 连接",
            },
        ],
    )
    embed = AsyncMock(side_effect=AssertionError("keyword mode must not embed"))
    monkeypatch.setattr(llm, "embed", embed)
    for query, chunk in [("退货期限", policy), ("数据库连接", database)]:
        [hit] = await search(
            store, gateway.client, [base.kb_id], query, retrieval={"mode": "keyword"}
        )
        assert hit.chunk_id == chunk.chunk_id and hit.score == 1
        assert hit.scores == {"vector": None, "keyword": 1, "rerank": None}
    assert (
        await search(
            store,
            gateway.client,
            [base.kb_id],
            "和 的 了 the and！",
            min_score=0,
            retrieval={"mode": "keyword"},
        )
        == []
    )
    embed.assert_not_awaited()
    assert gateway.requests == []


async def assert_hybrid_union(store, gateway, monkeypatch):
    base = await store.create(f"hybrid-{uuid4()}")
    chunks = await seed(
        store,
        base,
        [
            *[
                {"text": "数据库连接", "embedding": vector(0.9), "keywords": "数据库 连接"}
                for _ in range(20)
            ],
            {"text": "退货期限", "embedding": vector(0.6), "keywords": "退货 期限"},
        ],
    )
    query_vector = vector()
    monkeypatch.setattr(llm, "embed", AsyncMock(return_value=[query_vector]))
    target = chunks[-1]
    assert target.chunk_id not in await store.vector_candidates(base.kb_id, query_vector, 20)
    assert target.chunk_id in await store.keyword_candidates(base.kb_id, ["退货", "期限"], 20)
    [actual_vector] = await store.score_chunks(base.kb_id, [target.chunk_id], query_vector, None)
    [hit] = await search(
        store,
        gateway.client,
        [base.kb_id],
        "退货期限",
        top_k=1,
        min_score=0,
        retrieval={"mode": "hybrid"},
    )
    expected = sum(a * b for a, b in zip(query_vector, target.embedding, strict=True))
    assert hit.chunk_id == target.chunk_id
    assert hit.scores["vector"] == pytest.approx(expected)
    assert hit.scores["vector"] == pytest.approx(actual_vector.score)
    assert hit.scores["keyword"] == 1 and hit.scores["rerank"] is None
    assert hit.score == pytest.approx(0.7 * expected + 0.3)


async def test_hybrid_scores_both_routes_for_keyword_only_candidate(gateway, monkeypatch):
    await assert_hybrid_union(MemoryKnowledgeStore(), gateway, monkeypatch)


@pytest.mark.parametrize("weight", [0, 0.25, 1])
async def test_keyword_deduplication_weight_and_final_cutoff(gateway, monkeypatch, weight):
    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    await seed(store, base, [{"text": "退货", "keywords": "退货 退货", "embedding": vector(0.8)}])
    monkeypatch.setattr(llm, "embed", AsyncMock(return_value=[vector()]))
    expected = weight * 0.8 + (1 - weight) * 0.5
    [hit] = await search(
        store,
        gateway.client,
        [base.kb_id],
        "退货退货期限",
        min_score=0,
        retrieval={"mode": "hybrid", "vector_weight": weight},
    )
    assert hit.scores["keyword"] == 0.5 and hit.score == pytest.approx(expected)
    assert (
        await search(
            store,
            gateway.client,
            [base.kb_id],
            "退货退货期限",
            min_score=expected + 0.01,
            retrieval={"mode": "hybrid", "vector_weight": weight},
        )
        == []
    )


async def test_query_is_deduplicated_then_capped_at_32_terms_in_a_thread(gateway, monkeypatch):
    main_thread = threading.get_ident()

    def terms(text):
        assert threading.get_ident() != main_thread
        return ["repeat"] * 40 + [f"term{i}" for i in range(40)]

    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    await seed(store, base, [{"text": "资料", "keywords": "term30", "embedding": vector()}])
    monkeypatch.setattr(keywords, "terms", terms)
    [hit] = await search(
        store, gateway.client, [base.kb_id], "问题", min_score=0, retrieval={"mode": "keyword"}
    )
    assert hit.score == 1 / 32


@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid"])
async def test_all_modes_sort_ties_by_chunk_id_and_exclude_nonready(gateway, mode):
    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    [v] = await llm.embed(gateway.client, ["退货期限"], "mock-embed")
    ready = await seed(
        store,
        base,
        [{"text": "退货期限", "embedding": v, "keywords": "退货 期限"} for _ in range(3)],
    )
    hidden = await seed(
        store, base, [{"text": "退货期限", "embedding": v, "keywords": "退货 期限"}]
    )
    document_id = hidden[0].document_id
    for status in ["queued", "processing", "error"]:
        store._documents[document_id] = replace(store._documents[document_id], status=status)
        hits = await search(
            store, gateway.client, [base.kb_id], "退货期限", min_score=0, retrieval={"mode": mode}
        )
        assert [h.chunk_id for h in hits] == sorted(c.chunk_id for c in ready)


async def test_omitted_and_partial_retrieval_preserve_vector_results(gateway):
    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    [v] = await llm.embed(gateway.client, ["退货期限"], "mock-embed")
    await seed(
        store,
        base,
        [
            {"text": "退货期限", "embedding": v, "keywords": None},
            {"text": "别的内容", "embedding": vector(), "keywords": "退货 期限"},
        ],
    )
    legacy = await store.search(base.kb_id, v, 5, 0.3)
    for config in [None, {}, {"vector_weight": 0.2}, {"mode": "vector"}]:
        assert (
            await search(store, gateway.client, [base.kb_id], "退货期限", retrieval=config)
            == legacy
        )
    assert all(h.scores == {"vector": h.score, "keyword": None, "rerank": None} for h in legacy)
    with pytest.raises(MissingBase):
        await search(store, gateway.client, ["missing"], "退货", retrieval={"mode": "keyword"})


async def test_backfill_updates_only_null_keywords_and_keeps_vectors(gateway):
    store = MemoryKnowledgeStore()
    base = await store.create("旧资料")
    [v] = await llm.embed(gateway.client, ["退货期限"], "mock-embed")
    chunks = await seed(
        store,
        base,
        [
            {"text": "三十天", "heading": "退货期限", "embedding": v},
            {"text": "和 的 了", "embedding": v},
            {"text": "新资料", "embedding": v, "keywords": "已分词"},
        ],
    )
    before = await search(store, gateway.client, [base.kb_id], "退货期限")
    assert await store.keyword_candidates(base.kb_id, ["退货"], 20) == []
    assert await store.backfill_keywords(limit=1) == 1
    assert await store.backfill_keywords() == 1
    assert await store.backfill_keywords() == 0
    _, updated = await store.chunks(chunks[0].document_id, 0, 10)
    assert updated[0].keywords == keywords.chunk_keywords("三十天", "退货期限")
    assert updated[1].keywords == "" and updated[2].keywords == "已分词"
    assert [c.embedding for c in updated] == [c.embedding for c in chunks]
    assert await search(store, gateway.client, [base.kb_id], "退货期限") == before
    assert await store.keyword_candidates(base.kb_id, ["退货"], 20) == [chunks[0].chunk_id]


async def test_worker_backfills_until_short_batch_on_startup_and_each_minute(gateway, monkeypatch):
    store = MemoryKnowledgeStore()
    recover = AsyncMock(return_value=0)
    backfill = AsyncMock(side_effect=[500, 500, 3, 500, 0])
    monkeypatch.setattr(store, "recover_stale", recover)
    monkeypatch.setattr(store, "backfill_keywords", backfill)
    ticks = 0

    async def tick(seconds):
        nonlocal ticks
        assert seconds == 60
        ticks += 1
        if ticks == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("aep.knowledge.worker.asyncio.sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await KnowledgeWorker(store, gateway.client)._recover_loop()
    assert recover.await_count == 2
    assert backfill.await_count == 5
    assert all(call.kwargs == {"limit": 500} for call in backfill.await_args_list)


async def test_worker_tokenizes_heading_before_embedding_in_a_thread(
    gateway, tmp_path, monkeypatch
):
    main_thread = threading.get_ident()
    tokenized = threading.Event()
    original = keywords.terms

    def terms(text):
        assert threading.get_ident() != main_thread
        assert text.startswith("退货期限\n")
        tokenized.set()
        return original(text)

    original_embed = llm.embed

    async def embed(*args):
        assert tokenized.is_set()
        return await original_embed(*args)

    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    path = tmp_path / "doc.md"
    path.write_text(
        "# 退货期限\n\n商品收到后三十天内可申请退货，需要保持原包装并附上购物凭证。",
        encoding="utf-8",
    )
    document = Document(str(uuid4()), base.kb_id, path.name, "md", 100, str(path))
    await store.add_documents(base.kb_id, [document])
    monkeypatch.setattr(keywords, "terms", terms)
    monkeypatch.setattr(llm, "embed", embed)
    assert await KnowledgeWorker(store, gateway.client).process_once()
    _, [chunk] = await store.chunks(document.document_id, 0, 10)
    assert "退货" in chunk.keywords and "期限" in chunk.keywords
