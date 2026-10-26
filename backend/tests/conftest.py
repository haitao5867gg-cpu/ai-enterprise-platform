import json

import httpx
import pytest

from aep import llm
from aep.config import Settings

SETTINGS = Settings(llm_gateway_url="http://gateway.test", llm_gateway_key="sk-test")


class FakeGateway:
    """An in-process stand-in for LiteLLM that records requests and replies in turn."""

    def __init__(self, replies: list[str] | None = None, status: int = 200) -> None:
        self.replies = list(replies or ["你好"])
        self.status = status
        self.requests: list[dict] = []
        self.client = llm.make_client(
            SETTINGS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": request.headers, "body": body})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "provider key missing"}})
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
