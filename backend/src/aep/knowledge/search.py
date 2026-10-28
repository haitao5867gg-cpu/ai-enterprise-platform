"""Vector retrieval shared by the API and future workflow nodes."""

from openai import AsyncOpenAI

from aep import llm
from aep.knowledge.store import Hit, KnowledgeStore, MissingBase


async def search(
    store: KnowledgeStore,
    client: AsyncOpenAI,
    kb_ids: list[str],
    query: str,
    top_k: int = 5,
    min_score: float = 0.3,
) -> list[Hit]:
    """Take top_k per base, merge by cosine similarity, then take global top_k."""
    vectors: dict[str, list[float]] = {}
    hits: list[Hit] = []
    for kb_id in dict.fromkeys(kb_ids):
        base = await store.get(kb_id)
        if base is None:
            raise MissingBase(kb_id)
        if base.embedding_model not in vectors:
            vectors[base.embedding_model] = (
                await llm.embed(client, [query], base.embedding_model)
            )[0]
        hits.extend(await store.search(kb_id, vectors[base.embedding_model], top_k, min_score))
    return sorted(hits, key=lambda hit: (-hit.score, hit.chunk_id))[:top_k]
