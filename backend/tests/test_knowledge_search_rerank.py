"""Reranking behavior and gateway protocol, without network access."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from aep import llm
from aep.config import Settings
from aep.knowledge.rerank import RerankError, rerank, rerank_options
from aep.knowledge.search import search
from aep.knowledge.store import Hit, MemoryKnowledgeStore
from tests.conftest import SETTINGS, FakeGateway
from tests.test_knowledge_search import seed, vector


def test_rerank_options_use_configured_order_and_chat_aliases_only():
    assert rerank_options(
        Settings(rerank_models=["mock-rerank", "company-rerank"]),
        ["company", "mock-embed", "mock-rerank", "company-embed", "unused-rerank", "mock"],
    ) == [
        {"model": "mock-rerank", "kind": "rerank"},
        {"model": "company-rerank", "kind": "rerank"},
        {"model": "company", "kind": "llm"},
        {"model": "mock", "kind": "llm"},
    ]


@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid"])
async def test_mock_rerank_changes_order_and_filters_only_final_score(gateway, monkeypatch, mode):
    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    [first, second] = await seed(
        store,
        base,
        [
            {"text": "退货 期限", "embedding": vector(1), "keywords": "退货 期限"},
            {"text": "退货期限", "embedding": vector(0.1), "keywords": "退货"},
        ],
    )
    monkeypatch.setattr(llm, "embed", AsyncMock(return_value=[vector()]))
    initial = await search(
        store, gateway.client, [base.kb_id], "退货期限", min_score=0, retrieval={"mode": mode}
    )
    assert [h.chunk_id for h in initial] == [first.chunk_id, second.chunk_id]
    [hit] = await search(
        store,
        gateway.client,
        [base.kb_id],
        "退货期限",
        top_k=1,
        min_score=0.9,
        retrieval={"mode": mode, "rerank": {"model": "mock-rerank"}},
    )
    assert hit.chunk_id == second.chunk_id and hit.score == hit.scores["rerank"] == 1
    assert hit.scores["vector"] == (None if mode == "keyword" else pytest.approx(0.1))
    assert hit.scores["keyword"] == (None if mode == "vector" else 0.5)
    assert gateway.requests == []


async def test_llm_rerank_runs_once_after_merging_bases_and_numbers_truncated_candidates(
    monkeypatch,
):
    gateway = FakeGateway(['{"scores": [{"i": 2, "score": 9}, {"i": 1, "score": 2}]}'])
    store = MemoryKnowledgeStore()
    chunks = []
    bases = [await store.create("一"), await store.create("二")]
    for base, cosine in zip(bases, [1, 0.8], strict=True):
        chunks.extend(
            await seed(
                store,
                base,
                [{"text": "正文" * 500, "heading": "标题路径", "embedding": vector(cosine)}],
            )
        )
    monkeypatch.setattr(llm, "embed", AsyncMock(return_value=[vector()]))
    try:
        hits = await search(
            store,
            gateway.client,
            [b.kb_id for b in bases],
            "问题",
            min_score=0,
            retrieval={"rerank": {"model": "mock"}},
        )
        assert [h.chunk_id for h in hits] == [chunks[1].chunk_id, chunks[0].chunk_id]
        assert [h.score for h in hits] == [0.9, 0.2]
        assert [h.scores["vector"] for h in hits] == pytest.approx([0.8, 1])
        assert [h.scores["rerank"] for h in hits] == [0.9, 0.2]
        assert len(gateway.requests) == 1
        body = gateway.requests[0]["body"]
        assert body["model"] == "mock"
        prompt = json.loads(body["messages"][1]["content"])
        assert prompt["query"] == "问题"
        assert prompt["candidates"] == [
            {"i": i, "text": ("标题路径\n" + "正文" * 500)[:600]} for i in [1, 2]
        ]
    finally:
        await gateway.client.close()


def hit(text="退货期限", heading="政策") -> Hit:
    return Hit(
        "kb",
        "资料",
        "doc",
        "资料.md",
        "chunk",
        0,
        text,
        heading,
        None,
        0.8,
        scores={"vector": 0.8, "keyword": None, "rerank": None},
    )


async def test_llm_missing_scores_are_zero_and_invalid_reply_retries_once():
    gateway = FakeGateway(["不是JSON", '```json\n{"scores": [{"i": 2, "score": 7}]}\n```'])
    try:
        hits = await rerank(gateway.client, "问题", [hit(), hit()], "mock", "llm")
        assert [h.score for h in hits] == [0, 0.7]
        assert len(gateway.requests) == 2
        assert len(gateway.requests[1]["body"]["messages"]) == 4
    finally:
        await gateway.client.close()


@pytest.mark.parametrize(
    "reply",
    [
        "plain text",
        '{"wrong": []}',
        '{"scores": {}}',
        '{"scores": [{"i": 0, "score": 5}]}',
        '{"scores": [{"i": 2, "score": 5}]}',
        '{"scores": [{"i": true, "score": 5}]}',
        '{"scores": [{"i": 1, "score": -1}]}',
        '{"scores": [{"i": 1, "score": 11}]}',
        '{"scores": [{"i": 1, "score": "9"}]}',
        '{"scores": [{"i": 1, "score": true}]}',
        '{"scores": [{"i": 1, "score": NaN}]}',
        '{"scores": [{"i": 1, "score": 5}, {"i": 1, "score": 7}]}',
    ],
)
async def test_invalid_llm_scores_fail_after_exactly_one_retry(reply):
    gateway = FakeGateway([reply])
    try:
        with pytest.raises(RerankError, match="输出格式不正确"):
            await rerank(gateway.client, "问题", [hit()], "mock", "llm")
        assert len(gateway.requests) == 2
    finally:
        await gateway.client.close()


async def test_dedicated_rerank_uses_existing_transport_auth_and_trace_metadata(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )

    metadata = {"trace_id": "trace-75", "parent_observation_id": "node-75"}
    monkeypatch.setattr("aep.tracing.gateway_metadata", lambda: metadata)
    client = llm.make_client(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        hits = await rerank(client, "问题", [hit(), hit(heading="")], "company-rerank", "rerank")
        assert [h.score for h in hits] == [0.2, 0.9]
        [request] = requests
        assert str(request.url) == "http://gateway.test/rerank"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert json.loads(request.content) == {
            "model": "company-rerank",
            "query": "问题",
            "documents": ["政策\n退货期限", "退货期限"],
            "top_n": 2,
            "metadata": metadata,
        }
    finally:
        await client.close()


@pytest.mark.parametrize("failure", ["gateway", "timeout", "json", "indices", "range", "nan"])
async def test_dedicated_rerank_errors_are_never_silently_ignored(failure):
    def handle(request):
        if failure == "gateway":
            return httpx.Response(502, json={"error": {"message": "重排不可用"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("rerank timeout", request=request)
        if failure == "json":
            return httpx.Response(200, text="not json")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "index": 2 if failure == "indices" else 0,
                        "relevance_score": 1.2
                        if failure == "range"
                        else "NaN"
                        if failure == "nan"
                        else 0.5,
                    }
                ]
            },
        )

    client = llm.make_client(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(RerankError):
            await rerank(
                client.with_options(max_retries=0), "问题", [hit()], "company-rerank", "rerank"
            )
    finally:
        await client.close()


async def test_empty_results_do_not_call_rerank(gateway):
    assert await rerank(gateway.client, "问题", [], "mock", "llm") == []
    assert gateway.requests == []


@pytest.mark.parametrize(
    ("top_k", "candidates", "expected"), [(1, 5, 20), (6, 20, 24), (1, 50, 50)]
)
async def test_each_candidate_route_uses_pool_and_rerank_only_sees_requested_candidates(
    gateway, monkeypatch, top_k, candidates, expected
):
    store = MemoryKnowledgeStore()
    base = await store.create("资料")
    chunks = await seed(
        store,
        base,
        [{"text": "退货期限", "keywords": "退货 期限", "embedding": vector()} for _ in range(60)],
    )
    vector_candidates = AsyncMock(wraps=store.vector_candidates)
    keyword_candidates = AsyncMock(wraps=store.keyword_candidates)
    monkeypatch.setattr(store, "vector_candidates", vector_candidates)
    monkeypatch.setattr(store, "keyword_candidates", keyword_candidates)
    reranking = AsyncMock(side_effect=lambda client, query, hits, model, kind: hits)
    monkeypatch.setattr("aep.knowledge.search.rerank", reranking)
    hits = await search(
        store,
        gateway.client,
        [base.kb_id],
        "退货期限",
        top_k=top_k,
        min_score=0,
        retrieval={"mode": "hybrid", "rerank": {"model": "mock-rerank", "candidates": candidates}},
    )
    assert vector_candidates.call_args.args[-1] == expected
    assert keyword_candidates.call_args.args[-1] == expected
    assert len(reranking.call_args.args[2]) == candidates
    assert len(hits) == top_k
    assert {h.chunk_id for h in hits} <= {c.chunk_id for c in chunks}
