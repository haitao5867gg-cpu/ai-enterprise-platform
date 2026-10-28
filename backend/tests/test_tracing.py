"""Langfuse tracing, with spans captured in memory instead of sent to a server."""

import pytest
from langfuse import Langfuse
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tracing import Tracer, trace_id
from tests.conftest import SETTINGS, FakeGateway


def make_service(gateway: FakeGateway, tracer: Tracer | None = None) -> RunService:
    return RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(llm_client=gateway.client, settings=SETTINGS),
        tracer,
    )


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter, request):
    public_key = f"pk-{request.node.name}"  # Langfuse keeps one client per key
    client = Langfuse(
        public_key=public_key,
        secret_key="sk-test",
        base_url="http://langfuse.test",
        span_exporter=exporter,
        tracer_provider=TracerProvider(),
    )
    yield Tracer(client, public_key)
    client.shutdown()


def hex_id(value: int, width: int) -> str:
    return format(value, f"0{width}x")


async def test_run_is_one_trace_across_approval(tracer, exporter):
    gateway = FakeGateway(["回答"])
    service = make_service(gateway, tracer)

    record = await service.create("hello", {"text": "你好"})
    await service.wait_idle()
    await service.resume(record.run_id, "approve", None)
    await service.wait_idle()
    tracer.flush()

    spans = exporter.get_finished_spans()
    tid = trace_id(record.run_id)
    assert {hex_id(s.context.trace_id, 32) for s in spans} == {tid}
    # Segments are the top-level spans (a node may be called "start" too).
    ids = {s.context.span_id for s in spans}
    top = [s for s in spans if s.parent is None or s.parent.span_id not in ids]
    segments = sorted(top, key=lambda s: s.start_time)
    assert [s.name for s in segments] == ["start", "resume"]
    assert {"draft", "review"} <= {s.name for s in spans}

    # The gateway hangs its generation under the segment that made the call.
    assert gateway.requests[0]["body"]["metadata"] == {
        "existing_trace_id": tid,
        "parent_observation_id": hex_id(segments[0].context.span_id, 16),
        "generation_name": "draft",
    }
    assert (await service.get(record.run_id)).trace_id == tid


async def test_untraced_run_sends_no_trace_metadata():
    gateway = FakeGateway(["回答"])
    service = make_service(gateway)

    record = await service.create("hello", {"text": "你好"})
    await service.wait_idle()

    assert "metadata" not in gateway.requests[0]["body"]
    assert (await service.get(record.run_id)).trace_id is None


def test_tracer_is_off_without_keys():
    assert not Tracer.from_settings(SETTINGS).enabled
    half_configured = SETTINGS.model_copy(update={"langfuse_public_key": "pk"})
    assert not Tracer.from_settings(half_configured).enabled
