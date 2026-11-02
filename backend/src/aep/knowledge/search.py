"""Candidate retrieval, combined scoring and optional global reranking."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from openai import AsyncOpenAI

from aep import llm
from aep.config import Settings
from aep.knowledge import keywords
from aep.knowledge.rerank import rerank
from aep.knowledge.retrieval import Retrieval
from aep.knowledge.store import Hit, KnowledgeStore, MissingBase

log = logging.getLogger(__name__)


async def record_hits(store: KnowledgeStore, run_id: str | None, hits: list[Hit]) -> None:
    """Count the documents a run retrieved (docs/api/knowledge-governance.md). Recording is
    bookkeeping: a failure is logged and never fails the retrieval."""
    if not run_id or not hits:
        return
    try:
        await store.record_hits(run_id, [(hit.kb_id, hit.document_id) for hit in hits])
    except Exception:  # noqa: BLE001 - see docstring
        log.warning("recording knowledge hits for run %s failed", run_id, exc_info=True)


async def search(
    store: KnowledgeStore,
    client: AsyncOpenAI,
    kb_ids: list[str],
    query: str,
    top_k: int = 5,
    min_score: float = 0.3,
    retrieval: Retrieval | dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    check: Callable[[str], None] | None = None,
    run_id: str | None = None,
    include_inactive: bool = False,
) -> list[Hit]:
    """The best hits for ``query``. ``check`` is called with each model alias before a
    request goes to it, and raises when the model may not be used (aep.policy). Documents
    not in effect today are skipped unless ``include_inactive``; with ``run_id`` the hit
    documents count as used by that run."""
    config = Retrieval.model_validate(
        retrieval.model_dump()
        if isinstance(retrieval, Retrieval)
        else (retrieval if retrieval is not None else {}),
        context={"top_k": top_k},
    )
    pool = max(20, top_k * 4, config.rerank.candidates if config.rerank else 0)
    words = None
    if config.mode != "vector":
        words = list(dict.fromkeys(await asyncio.to_thread(keywords.terms, query)))[:32]
    vectors: dict[str, list[float]] = {}
    hits: list[Hit] = []
    for kb_id in dict.fromkeys(kb_ids):
        base = await store.get(kb_id)
        if base is None:
            raise MissingBase(kb_id)
        vector = None
        candidates: list[str] = []
        if config.mode != "keyword":
            if base.embedding_model not in vectors:
                if check is not None:
                    check(base.embedding_model)
                vectors[base.embedding_model] = (
                    await llm.embed(client, [query], base.embedding_model)
                )[0]
            vector = vectors[base.embedding_model]
            candidates.extend(
                await store.vector_candidates(
                    kb_id, vector, pool, include_inactive=include_inactive
                )
            )
        if words is not None:
            candidates.extend(
                await store.keyword_candidates(
                    kb_id, words, pool, include_inactive=include_inactive
                )
            )
        scored = await store.score_chunks(
            kb_id,
            list(dict.fromkeys(candidates)),
            vector,
            words,
            include_inactive=include_inactive,
        )
        for hit in scored:
            score = hit.score
            if config.mode == "hybrid":
                score = (
                    config.vector_weight * hit.scores["vector"]
                    + (1 - config.vector_weight) * hit.scores["keyword"]
                )
            hits.append(replace(hit, score=score))
    hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    if config.rerank:
        model = config.rerank.model
        if check is not None:
            check(model)
        kind = "rerank" if model in (settings or Settings()).rerank_models else "llm"
        hits = await rerank(client, query, hits[: config.rerank.candidates], model, kind)
        hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    found = [hit for hit in hits if hit.score >= min_score][:top_k]
    await record_hits(store, run_id, found)
    return found
