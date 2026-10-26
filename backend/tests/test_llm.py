import json

import httpx
from fastapi.testclient import TestClient

from aep import llm
from aep.config import Settings
from aep.main import app, get_llm_client

SETTINGS = Settings(llm_gateway_url="http://gateway.test", llm_gateway_key="sk-test")


def fake_gateway(status: int = 200, text: str = "你好"):
    """An in-process stand-in for LiteLLM that records the requests it gets."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "provider key missing"}})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    client = llm.make_client(
        SETTINGS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return client, seen


async def test_chat_goes_through_gateway_with_alias():
    client, seen = fake_gateway()
    result = await llm.chat(client, [{"role": "user", "content": "hi"}], model="qwen")

    assert result.text == "你好"
    assert result.model == "qwen"
    assert result.usage.total_tokens == 5
    req = seen[0]
    assert str(req.url) == "http://gateway.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    assert json.loads(req.content)["model"] == "qwen"


def test_chat_endpoint_uses_default_model():
    client, seen = fake_gateway()
    app.dependency_overrides[get_llm_client] = lambda: client
    try:
        resp = TestClient(app).post(
            "/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["model"] == "mock"
    assert json.loads(seen[0].content)["model"] == "mock"


def test_chat_endpoint_maps_gateway_errors_to_502():
    client, _ = fake_gateway(status=400)
    app.dependency_overrides[get_llm_client] = lambda: client
    try:
        resp = TestClient(app).post(
            "/llm/chat",
            json={"model": "gpt", "messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 502
    assert "provider key missing" in resp.json()["detail"]
