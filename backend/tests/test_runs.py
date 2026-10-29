"""Runs API and runtime, on in-memory storage. Postgres-backed tests: test_runs_postgres.py."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

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


async def test_runs_say_what_they_are_about_who_asked_and_where_they_are(api, service):
    text = "  请把\n上个月的订单   按城市汇总，" + "并且" * 60
    resp = await api.post(
        "/runs", json={"app": "hello", "input": {"text": text}, "requester": "  李雷 "}
    )
    await service.wait_idle()
    run_id = resp.json()["run_id"]

    listed = (await api.get("/runs")).json()[0]
    detail = (await api.get(f"/runs/{run_id}")).json()
    for body in (listed, detail):
        assert body["app_title"] == "问答示例"
        assert body["title"].startswith("请把 上个月的订单 按城市汇总，并且")
        assert len(body["title"]) == 80 and body["title"].endswith("…")
        assert body["created_by"] == "李雷"
        assert body["step"] == {"node": "review", "title": "确认"}
        assert body["waiting_since"] is not None
        assert body["wait_seconds"] == 0
        assert body["finished_at"] is None
    assert detail["created_at"] == listed["created_at"]

    await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    await service.wait_idle()
    done = (await api.get(f"/runs/{run_id}")).json()
    assert done["step"] == {"node": "end", "title": "结束"}
    assert done["waiting_since"] is None
    assert done["wait_seconds"] > 0
    assert done["finished_at"] == done["updated_at"]


async def test_requester_is_optional_and_short(api, service):
    blank = await api.post(
        "/runs", json={"app": "hello", "input": {"text": "你好"}, "requester": "   "}
    )
    await service.wait_idle()
    assert (await api.get(f"/runs/{blank.json()['run_id']}")).json()["created_by"] is None

    long = await api.post(
        "/runs", json={"app": "hello", "input": {"text": "你好"}, "requester": "李" * 101}
    )
    assert long.status_code == 422


async def test_code_app_runs_are_titled_by_their_request(service):
    record = await service._store.create("etl", {"datasource_id": "abc", "request": "汇总订单"})
    assert service.title(record) == "汇总订单"
    assert (await service.app_titles())["etl"] == "自然语言 ETL 工作流"
    assert service.title(replace(record, input={"datasource_id": "abc"})) is None


async def test_waits_add_up_across_approvals():
    store = MemoryRunStore()
    record = await store.create("hello", {"text": "你好"})
    await store.set_status(record.run_id, "waiting")
    first = (await store.get(record.run_id)).waiting_since
    # Waiting again (a second interrupt in a row) keeps the wait that began first.
    await store.set_status(record.run_id, "waiting")
    assert (await store.get(record.run_id)).waiting_since == first

    store._runs[record.run_id] = replace(
        store._runs[record.run_id], waiting_since=datetime.now(UTC) - timedelta(minutes=2)
    )
    await store.set_status(record.run_id, "running")
    after = await store.get(record.run_id)
    assert after.waiting_since is None
    assert 120 <= after.wait_seconds < 130

    await store.set_status(record.run_id, "waiting")
    store._runs[record.run_id] = replace(
        store._runs[record.run_id], waiting_since=datetime.now(UTC) - timedelta(minutes=1)
    )
    await store.set_status(record.run_id, "done")
    assert 180 <= (await store.get(record.run_id)).wait_seconds < 190

    # Runs from before waits were counted stay unknown.
    store._runs[record.run_id] = replace(store._runs[record.run_id], wait_seconds=None)
    await store.set_status(record.run_id, "waiting")
    await store.set_status(record.run_id, "running")
    assert (await store.get(record.run_id)).wait_seconds is None


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
