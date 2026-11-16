import email
import email.policy
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
        # What /v1/audio/transcriptions answers (docs/api/transcription.md).
        self.transcript: dict = dict(TRANSCRIPT)
        self.client = llm.make_client(
            SETTINGS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            return self._transcribe(request)
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

    def _transcribe(self, request: httpx.Request) -> httpx.Response:
        """A multipart transcription request: its form fields and the file's name and size
        are recorded."""
        content_type = request.headers["content-type"]
        message = email.message_from_bytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + request.content,
            policy=email.policy.HTTP,
        )
        form: dict = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True)
            if part.get_filename():
                form[name] = {"filename": part.get_filename(), "size": len(data)}
            else:
                form.setdefault(name, []).append(data.decode())
        body = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in form.items()}
        self.requests.append({"url": str(request.url), "headers": request.headers, "body": body})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "provider key missing"}})
        return httpx.Response(200, json=self.transcript)


# A verbose_json answer, as faster-whisper (speaches) gives it.
TRANSCRIPT = {
    "task": "transcribe",
    "language": "zh",
    "duration": 4.7706875,
    "text": " 冬天减脂比夏天难吗？ 关键是热量缺口。",
    "segments": [
        {"id": 1, "start": 0.0, "end": 2.28, "text": " 冬天减脂比夏天难吗？"},
        {"id": 2, "start": 2.78, "end": 4.12, "text": " 关键是热量缺口。"},
        {"id": 3, "start": 4.2, "end": 4.3, "text": "  "},
    ],
    "words": [
        {"start": 0.0, "end": 0.28, "word": " 冬天", "probability": 0.9},
        {"start": 0.28, "end": 0.62, "word": "减脂", "probability": 0.9},
        {"start": 2.78, "end": 3.1, "word": " 关键", "probability": 0.9},
    ],
}


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


def said(messages: list[dict]) -> list[tuple]:
    """(role, node, actor, content) of each message; every message names its step and time."""
    for m in messages:
        assert m["node_title"] and datetime.fromisoformat(m["at"])
    return [(m["role"], m["node"], m["actor"], m["content"]) for m in messages]
