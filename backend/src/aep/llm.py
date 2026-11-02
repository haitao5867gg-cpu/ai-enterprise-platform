"""Model calls through the LiteLLM gateway.

Callers name a model alias from infra/litellm/config.yaml (``mock``, ``gpt``,
``claude-bedrock``, ``qwen``, ``company``, ...); the gateway maps it to a provider. Nothing
else in the platform talks to a model provider directly.
"""

import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import openai
from langgraph.config import get_config
from openai import AsyncOpenAI

from aep import tracing
from aep.config import Settings

log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class CallRecord:
    """One model call the platform made, for the model page and usage reports
    (docs/api/models.md, "调用记录")."""

    model: str
    kind: str  # "chat" | "embedding"
    run_id: str | None
    node: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    ok: bool
    error: str | None  # a problem code (``error_code``) when the call failed
    duration_ms: int


Recorder = Callable[[CallRecord], Awaitable[None]]
_recorder: Recorder | None = None


def set_recorder(recorder: Recorder | None) -> None:
    """Where every model call is recorded (the platform's ``model_calls`` table); None
    records nothing (tests, scripts)."""
    global _recorder
    _recorder = recorder


def _run_context() -> tuple[str | None, str | None]:
    """The run and graph node the current call is made from, when it is made in one."""
    try:
        config = get_config()
    except RuntimeError:  # not inside a graph step
        return None, None
    run_id = (config.get("configurable") or {}).get("thread_id")
    node = (config.get("metadata") or {}).get("langgraph_node")
    return (str(run_id) if run_id else None), (str(node) if node else None)


async def _record(
    model: str,
    kind: str,
    started: float,
    *,
    usage: Any = None,
    cost: float | None = None,
    error: BaseException | None = None,
) -> None:
    if _recorder is None:
        return
    run_id, node = _run_context()
    record = CallRecord(
        model=model,
        kind=kind,
        run_id=run_id,
        node=node,
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        cost_usd=cost,
        ok=error is None,
        error=error_code(error) if error is not None else None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    try:
        await _recorder(record)
    except Exception:  # noqa: BLE001 - recording must never break the call
        log.warning("could not record a call to model %s", model, exc_info=True)


def _cost(headers: httpx.Headers) -> float | None:
    """What the gateway says the call cost in USD, when it knows."""
    value = headers.get("x-litellm-response-cost")
    try:
        cost = float(value) if value is not None else None
    except ValueError:
        return None
    return cost if cost is not None and math.isfinite(cost) and cost >= 0 else None


def error_code(exc: BaseException) -> str:
    """Which problem a failed model call had (docs/api/models.md, "问题分类")."""
    if isinstance(exc, openai.APITimeoutError):
        return "timeout"
    if isinstance(exc, openai.APIConnectionError):
        return "gateway"
    if not isinstance(exc, openai.APIStatusError):
        return "dimensions" if "向量维度" in str(exc) else "gateway"
    text = str(exc.message).lower()
    if exc.status_code in (401, 403) or "authentication" in text:
        return "auth"
    if exc.status_code == 429 or any(
        word in text for word in ("credit balance", "insufficient", "quota", "rate limit")
    ):
        return "quota"
    if exc.status_code == 404 or any(
        word in text for word in ("not_found", "not found", "does not exist", "invalid model name")
    ):
        return "not_found"
    if exc.status_code == 408 or "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection error" in text or "connecterror" in text or "connection refused" in text:
        return "unreachable"
    if exc.status_code in (400, 422):
        return "bad_request"
    return "gateway"


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
    started = time.monotonic()
    try:
        raw = await client.chat.completions.with_raw_response.create(
            model=model, messages=messages, **params
        )
        resp = raw.parse()
    except Exception as exc:
        await _record(model, "chat", started, error=exc)
        raise
    usage = resp.usage
    await _record(model, "chat", started, usage=usage, cost=_cost(raw.headers))
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
        started = time.monotonic()
        try:
            raw = await client.embeddings.with_raw_response.create(
                model=model,
                input=batch,
                dimensions=EMBEDDING_DIMENSIONS,
                extra_body={"metadata": trace_meta} if trace_meta else None,
            )
            response = raw.parse()
            vectors.extend(_vectors(response, len(batch)))
        except Exception as exc:
            await _record(model, "embedding", started, error=exc)
            raise
        await _record(model, "embedding", started, usage=response.usage, cost=_cost(raw.headers))
    return vectors


def _vectors(response: Any, count: int) -> list[list[float]]:
    data = sorted(response.data, key=lambda item: item.index)
    if [item.index for item in data] != list(range(count)):
        raise ValueError("向量模型返回的向量数量或顺序不正确")
    vectors = []
    for item in data:
        if len(item.embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"向量维度必须为 {EMBEDDING_DIMENSIONS}，收到 {len(item.embedding)}")
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
