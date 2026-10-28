import hashlib
import math

import pytest

from aep import llm
from tests.conftest import FakeGateway


async def test_mock_embedding_is_stable_normalized_and_local(gateway):
    texts = ["退货期限", "退货的期限是多久", "数据库连接", "", "字"]
    vectors = await llm.embed(gateway.client, texts, "mock-embed")
    assert vectors == await llm.embed(gateway.client, texts, "mock-embed")
    assert gateway.requests == []
    assert all(len(vector) == 1024 for vector in vectors)
    assert vectors[3] == [0.0] * 1024
    assert math.sqrt(sum(v * v for v in vectors[0])) == pytest.approx(1)

    def similarity(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert similarity(vectors[0], vectors[1]) > similarity(vectors[0], vectors[2])
    # A fixed hashlib location catches accidental use of process-randomized hash().
    index = int.from_bytes(hashlib.sha256("字".encode()).digest()[:8]) % 1024
    assert vectors[4][index] == 1


async def test_gateway_batches_dimensions_metadata_and_input_order(gateway, monkeypatch):
    monkeypatch.setattr("aep.tracing.gateway_metadata", lambda: {"trace_id": "test-trace"})
    texts = [f"第{i}段" for i in range(23)]
    result = await llm.embed(gateway.client, texts, "company-embed")
    assert len(result) == 23
    assert [len(r["body"]["input"]) for r in gateway.requests] == [10, 10, 3]
    for request in gateway.requests:
        assert request["url"] == "http://gateway.test/v1/embeddings"
        assert request["body"]["model"] == "company-embed"
        assert request["body"]["dimensions"] == 1024
        assert request["body"]["metadata"] == {"trace_id": "test-trace"}
    for text, vector in zip(texts, result, strict=True):
        assert vector[sum(map(ord, text)) % 1024] == 1


async def test_wrong_embedding_dimensions_fail():
    gateway = FakeGateway(embedding_dimensions=512)
    async with gateway.client:
        with pytest.raises(ValueError, match="向量维度必须为 1024"):
            await llm.embed(gateway.client, ["资料"], "company-embed")


async def test_empty_embedding_batch_makes_no_request(gateway):
    assert await llm.embed(gateway.client, [], "company-embed") == []
    assert not gateway.requests
