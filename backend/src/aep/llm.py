"""Model calls through the LiteLLM gateway.

Callers name a model alias from infra/litellm/config.yaml (``mock``, ``gpt``,
``claude-bedrock``, ``qwen``, ``company``, ...); the gateway maps it to a provider. Nothing
else in the platform talks to a model provider directly.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI

from aep import tracing
from aep.config import Settings


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: str
    usage: Usage


def make_client(settings: Settings, http_client: httpx.AsyncClient | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=f"{settings.llm_gateway_url.rstrip('/')}/v1",
        api_key=settings.llm_gateway_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
        http_client=http_client,
    )


async def chat(
    client: AsyncOpenAI,
    messages: list[dict[str, str]],
    model: str,
    **params: object,
) -> ChatResult:
    """Send one chat completion through the gateway and return its text and usage."""
    trace_meta = tracing.gateway_metadata()
    if trace_meta:
        extra_body = dict(params.pop("extra_body", None) or {})
        extra_body["metadata"] = {**extra_body.get("metadata", {}), **trace_meta}
        params["extra_body"] = extra_body
    resp = await client.chat.completions.create(model=model, messages=messages, **params)
    usage = resp.usage
    return ChatResult(
        text=resp.choices[0].message.content or "",
        model=model,
        usage=Usage(
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
        ),
    )


EMBEDDING_DIMENSIONS = 1024
EMBEDDING_BATCH_SIZE = 10


def _mock_embedding(text: str) -> list[float]:
    counts = [0.0] * EMBEDDING_DIMENSIONS
    grams = [text] if len(text) == 1 else [text[i : i + 2] for i in range(len(text) - 1)]
    for gram in grams:
        index = int.from_bytes(hashlib.sha256(gram.encode("utf-8")).digest()[:8])
        counts[index % EMBEDDING_DIMENSIONS] += 1
    norm = math.sqrt(sum(value * value for value in counts))
    return [value / norm for value in counts] if norm else counts


async def embed(client: AsyncOpenAI, texts: list[str], model: str) -> list[list[float]]:
    """Embed at most ten texts per gateway request, preserving input order."""
    if model == "mock-embed":
        return [_mock_embedding(text) for text in texts]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[start : start + EMBEDDING_BATCH_SIZE]
        trace_meta = tracing.gateway_metadata()
        response = await client.embeddings.create(
            model=model,
            input=batch,
            dimensions=EMBEDDING_DIMENSIONS,
            extra_body={"metadata": trace_meta} if trace_meta else None,
        )
        data = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in data] != list(range(len(batch))):
            raise ValueError("向量模型返回的向量数量或顺序不正确")
        for item in data:
            if len(item.embedding) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"向量维度必须为 {EMBEDDING_DIMENSIONS}，收到 {len(item.embedding)}"
                )
            if not all(math.isfinite(value) for value in item.embedding):
                raise ValueError("向量模型返回了无效数值")
            vectors.append(item.embedding)
    return vectors


def parse_json(text: str) -> Any:
    """The JSON object in a model reply, tolerating code fences and text around it."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end < start:
        raise ValueError("回复里没有 JSON 对象")
    return json.loads(candidate[start : end + 1])
