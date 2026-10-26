"""Run tracing in Langfuse.

Every run is one Langfuse trace whose id is derived from the run id. Each time
the run executes (start, resume after a decision, recovery after a restart) it
adds a span to that trace, with LangGraph's nodes underneath. Model calls are
reported by the gateway (LiteLLM), which attaches each generation to the span
that was active when the call was made; see ``gateway_metadata``.

Without Langfuse keys the tracer does nothing and nothing leaves the process.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from langgraph.config import get_config
from opentelemetry import trace as otel_trace

from aep.config import Settings


def trace_id(run_id: str) -> str:
    """The Langfuse trace id of a run: stable, so every segment lands on one trace."""
    return Langfuse.create_trace_id(seed=run_id)


@dataclass
class Segment:
    """One execution of a run. ``callbacks`` go into the LangGraph config."""

    span: Any = None
    callbacks: list[Any] = field(default_factory=list)

    def finish(self, output: Any = None, error: str | None = None) -> None:
        if self.span is None:
            return
        if error is None:
            self.span.update(output=output)
        else:
            self.span.update(output=output, level="ERROR", status_message=error)


class Tracer:
    def __init__(self, client: Langfuse | None = None, public_key: str | None = None) -> None:
        self._client = client
        self._public_key = public_key

    @classmethod
    def from_settings(cls, settings: Settings) -> "Tracer":
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            return cls()
        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_host,
            environment=settings.env,
        )
        return cls(client, settings.langfuse_public_key)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @contextmanager
    def segment(self, run_id: str, app: str, name: str, input: Any = None) -> Iterator[Segment]:
        if self._client is None:
            yield Segment()
            return
        with self._client.start_as_current_observation(
            trace_context={"trace_id": trace_id(run_id)},
            name=name,
            as_type="span",
            input=input,
        ) as span:
            with propagate_attributes(trace_name=app, metadata={"run_id": run_id}):
                yield Segment(span, [CallbackHandler(public_key=self._public_key)])

    def flush(self) -> None:
        if self._client is not None:
            self._client.flush()

    def shutdown(self) -> None:
        if self._client is not None:
            self._client.shutdown()


def gateway_metadata() -> dict[str, str]:
    """Request metadata that tells the gateway where to attach its generation.

    Empty outside a traced run, so untraced calls send nothing extra.
    """
    ctx = otel_trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    meta = {
        "existing_trace_id": format(ctx.trace_id, "032x"),
        "parent_observation_id": format(ctx.span_id, "016x"),
    }
    node = _langgraph_node()
    if node:
        meta["generation_name"] = node
    return meta


def _langgraph_node() -> str | None:
    try:
        return get_config().get("metadata", {}).get("langgraph_node")
    except RuntimeError:  # not inside a graph step
        return None
