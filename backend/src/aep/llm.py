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
    kind: str  # "chat" | "embedding" | "rerank" | "transcription"
    run_id: str | None
    node: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    ok: bool
    error: str | None  # a problem code (``error_code``) when the call failed
    duration_ms: int
    # How long the audio of a transcription was, when the model said.
    audio_seconds: float | None = None


Recorder = Callable[[CallRecord], Awaitable[None]]
_recorder: Recorder | None = None


def set_recorder(recorder: Recorder | None) -> None:
    """Where every model call is recorded (the platform's ``model_calls`` table); None
    records nothing (tests, scripts)."""
    global _recorder
    _recorder = recorder


class BudgetExceeded(RuntimeError):
    """The run's app has used up its monthly model budget (docs/api/ops.md, 「预算」);
    the call was not sent."""


# Called with the run a model call is made from before it is sent; raises
# BudgetExceeded to stop it. None checks nothing (tests, scripts).
Guard = Callable[[str | None], Awaitable[None]]
_guard: Guard | None = None


def set_guard(guard: Guard | None) -> None:
    global _guard
    _guard = guard


async def _check() -> None:
    if _guard is not None:
        await _guard(run_context()[0])


def run_context() -> tuple[str | None, str | None]:
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
    audio_seconds: float | None = None,
) -> None:
    if _recorder is None:
        return
    run_id, node = run_context()
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
        audio_seconds=audio_seconds,
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
    await _check()
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


@dataclass(frozen=True)
class Transcript:
    """What a transcription model heard (docs/api/transcription.md): the whole text, each
    sentence and, when the model gives them, each word, with times in seconds."""

    text: str
    language: str | None
    duration_seconds: float | None
    segments: list[dict[str, Any]]
    words: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "segments": self.segments,
            "words": self.words,
        }


async def transcribe(
    client: AsyncOpenAI,
    file: tuple[str, bytes],
    model: str,
    *,
    language: str | None = None,
    prompt: str | None = None,
) -> Transcript:
    """Send one audio (or video) file to a transcription model through the gateway, asking
    for sentence and word times (``verbose_json``)."""
    await _check()
    params: dict[str, Any] = {}
    if language:
        params["language"] = language
    if prompt:
        params["prompt"] = prompt
    started = time.monotonic()
    try:
        raw = await client.audio.transcriptions.with_raw_response.create(
            model=model,
            file=file,
            response_format="verbose_json",
            timestamp_granularities=["segment", "word"],
            **params,
        )
        found = transcript(raw.http_response.json(), language)
    except Exception as exc:
        await _record(model, "transcription", started, error=exc)
        raise
    await _record(
        model,
        "transcription",
        started,
        cost=_cost(raw.headers),
        audio_seconds=found.duration_seconds,
    )
    return found


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return round(number, 3) if math.isfinite(number) and number >= 0 else None


def _timed(items: Any, key: str) -> list[dict[str, Any]]:
    """Timed pieces (sentences under ``text``, words under ``word``) in time order, with
    blank ones and ones without times left out."""
    found = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get(key), str):
            continue
        text = item[key].strip()
        start, end = _seconds(item.get("start")), _seconds(item.get("end"))
        if text and start is not None and end is not None:
            found.append({"start": start, "end": max(start, end), key: text})
    return sorted(found, key=lambda piece: (piece["start"], piece["end"]))


def transcript(data: Any, language: str | None = None) -> Transcript:
    """A transcription model's ``verbose_json`` answer, tidied."""
    if not isinstance(data, dict):
        raise ValueError("转写模型的回答格式不对")
    segments = _timed(data.get("segments"), "text")
    words = _timed(data.get("words"), "word")
    if not words:  # some servers give words only inside each sentence
        nested = [w for s in data.get("segments") or [] if isinstance(s, dict)
                  for w in s.get("words") or []]  # fmt: skip
        words = _timed(nested, "word")
    text = data.get("text")
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        text = "".join(s["text"] for s in segments)
    said = data.get("language")
    duration = _seconds(data.get("duration"))
    if duration is None and segments:
        duration = max(s["end"] for s in segments)
    return Transcript(
        text=text,
        language=said.strip() if isinstance(said, str) and said.strip() else language,
        duration_seconds=duration,
        segments=segments,
        words=words,
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
    await _check()
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
