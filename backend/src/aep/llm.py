"""Model calls through the LiteLLM gateway.

Callers name a model alias from infra/litellm/config.yaml (``mock``, ``gpt``,
``claude-bedrock``, ``qwen``, ...); the gateway maps it to a provider. Nothing
else in the platform talks to a model provider directly.
"""

from dataclasses import dataclass

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
