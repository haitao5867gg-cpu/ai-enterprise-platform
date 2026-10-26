from fastapi.testclient import TestClient

from aep import llm
from aep.main import app, get_llm_client
from tests.conftest import FakeGateway


async def test_chat_goes_through_gateway_with_alias(gateway):
    result = await llm.chat(gateway.client, [{"role": "user", "content": "hi"}], model="qwen")

    assert result.text == "你好"
    assert result.model == "qwen"
    assert result.usage.total_tokens == 5
    req = gateway.requests[0]
    assert req["url"] == "http://gateway.test/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer sk-test"
    assert req["body"]["model"] == "qwen"


def test_chat_endpoint_uses_default_model(gateway):
    app.dependency_overrides[get_llm_client] = lambda: gateway.client
    try:
        resp = TestClient(app).post(
            "/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["model"] == "mock"
    assert gateway.requests[0]["body"]["model"] == "mock"


def test_chat_endpoint_maps_gateway_errors_to_502():
    gateway = FakeGateway(status=400)
    app.dependency_overrides[get_llm_client] = lambda: gateway.client
    try:
        resp = TestClient(app).post(
            "/llm/chat",
            json={"model": "gpt", "messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 502
    assert "provider key missing" in resp.json()["detail"]
