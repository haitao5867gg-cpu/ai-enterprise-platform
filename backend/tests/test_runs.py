"""Runs API and runtime, on in-memory storage. Postgres-backed tests: test_runs_postgres.py."""

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.main import app
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from tests.conftest import SETTINGS, FakeGateway


@pytest.fixture
def gateway():
    return FakeGateway(replies=["第一版回答", "修改后的回答"])


@pytest.fixture
def service(gateway):
    return RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(llm_client=gateway.client, settings=SETTINGS),
    )


@pytest.fixture
async def api(service):
    app.state.run_service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def start(api, service, text="你好") -> str:
    resp = await api.post("/runs", json={"app": "hello", "input": {"text": text}})
    assert resp.status_code == 201
    await service.wait_idle()
    return resp.json()["run_id"]


async def test_create_runs_until_approval(api, service, gateway):
    run_id = await start(api, service)

    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "waiting"
    assert body["messages"] == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "第一版回答"},
    ]
    assert body["pending"]["kind"] == "approval"
    assert body["pending"]["options"] == ["approve", "reject"]
    assert body["result"] is None
    assert gateway.requests[0]["body"]["model"] == "mock"


async def test_approve_finishes_run(api, service):
    run_id = await start(api, service)

    resp = await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert resp.status_code == 202
    await service.wait_idle()

    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "done"
    assert body["pending"] is None
    assert body["result"] == {"answer": "第一版回答"}


async def test_reject_redrafts_with_feedback_and_waits_again(api, service, gateway):
    run_id = await start(api, service)

    await api.post(f"/runs/{run_id}/resume", json={"decision": "reject", "comment": "更短一点"})
    await service.wait_idle()

    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "waiting"
    assert body["messages"][-2:] == [
        {"role": "user", "content": "驳回：更短一点"},
        {"role": "assistant", "content": "修改后的回答"},
    ]
    second_prompt = gateway.requests[1]["body"]["messages"]
    assert "更短一点" in second_prompt[-1]["content"]


async def test_resume_rules(api, service):
    run_id = await start(api, service)

    bad = await api.post(f"/runs/{run_id}/resume", json={"decision": "maybe"})
    assert bad.status_code == 422

    await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    await service.wait_idle()
    again = await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert again.status_code == 409

    missing = await api.post("/runs/00000000-0000-0000-0000-000000000000/resume", json={})
    assert missing.status_code == 422  # body validation runs before lookup
    assert (await api.get("/runs/nope")).status_code == 404


async def test_unknown_app_and_listing(api, service):
    resp = await api.post("/runs", json={"app": "nope"})
    assert resp.status_code == 404

    run_id = await start(api, service)
    runs = (await api.get("/runs")).json()
    assert [r["run_id"] for r in runs] == [run_id]
    assert runs[0]["status"] == "waiting"


async def test_model_failure_marks_run_as_error(service):
    failing = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(llm_client=FakeGateway(status=500).client, settings=SETTINGS),
    )
    record = await failing.create("hello", {"text": "hi"})
    await failing.wait_idle()

    view = await failing.get(record.run_id)
    assert view.record.status == "error"
    assert view.record.error
