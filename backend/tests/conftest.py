import json
from datetime import datetime

import httpx
import pytest

from aep import llm
from aep.config import Settings

SETTINGS = Settings(llm_gateway_url="http://gateway.test", llm_gateway_key="sk-test")


class FakeGateway:
    """An in-process stand-in for LiteLLM that records requests and replies in turn."""

    def __init__(
        self,
        replies: list[str] | None = None,
        status: int = 200,
        *,
        embedding_status: int | None = None,
        embedding_dimensions: int = 1024,
        embedding_statuses: list[int] | None = None,
    ) -> None:
        self.replies = list(replies or ["你好"])
        self.status = status
        self.embedding_status = embedding_status
        self.embedding_dimensions = embedding_dimensions
        self.embedding_statuses = list(embedding_statuses or [])
        self.requests: list[dict] = []
        self.client = llm.make_client(
            SETTINGS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": request.headers, "body": body})
        embeddings = request.url.path == "/v1/embeddings"
        status = self.status
        if embeddings:
            status = self.embedding_status if self.embedding_status is not None else self.status
            if self.embedding_statuses:
                status = self.embedding_statuses.pop(0)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "provider key missing"}})
        if embeddings:
            data = []
            for index, text in enumerate(body["input"]):
                vector = [0.0] * self.embedding_dimensions
                vector[sum(map(ord, text)) % len(vector)] = 1.0
                data.append({"object": "embedding", "index": index, "embedding": vector})
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": body["model"],
                    "data": data,
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
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


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


def said(messages: list[dict]) -> list[tuple]:
    """(role, node, actor, content) of each message; every message names its step and time."""
    for m in messages:
        assert m["node_title"] and datetime.fromisoformat(m["at"])
    return [(m["role"], m["node"], m["actor"], m["content"]) for m in messages]
