"""Runs API and runtime, on in-memory storage. Postgres-backed tests: test_runs_postgres.py."""

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.main import app
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import NotCancellable, NotWaiting, RunService, _config
from aep.runtime.store import MemoryRunStore
from tests.conftest import SETTINGS, FakeGateway, said


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


async def start(api, service, text="你好", requester=None) -> str:
    resp = await api.post(
        "/runs", json={"app": "hello", "input": {"text": text}, "requester": requester}
    )
    assert resp.status_code == 201
    await service.wait_idle()
    return resp.json()["run_id"]


async def test_create_runs_until_approval(api, service, gateway):
    run_id = await start(api, service, requester="李雷")

    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "waiting"
    assert said(body["messages"]) == [
        ("user", "start", "李雷", "你好"),
        ("assistant", "draft", None, "第一版回答"),
    ]
    assert [m["node_title"] for m in body["messages"]] == ["开始", "回答"]
    assert body["pending"]["kind"] == "approval"
    assert body["pending"]["options"] == ["approve", "reject"]
    assert body["result"] is None
    assert gateway.requests[0]["body"]["model"] == "mock"


async def test_approve_finishes_run(api, service):
    run_id = await start(api, service)
    waited = (await api.get(f"/runs/{run_id}")).json()["messages"]

    decided = datetime.now(UTC)
    resp = await api.post(f"/runs/{run_id}/resume", json={"decision": "approve", "actor": "韩梅梅"})
    assert resp.status_code == 202
    await service.wait_idle()

    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "done"
    assert body["pending"] is None
    assert body["result"] == {"answer": "第一版回答"}
    assert said(body["messages"][-1:]) == [("user", "review", "韩梅梅", "选了「确认」")]
    # A decision is timed when it is made, not when the step started waiting.
    at = datetime.fromisoformat(body["messages"][-1]["at"])
    assert at >= decided > datetime.fromisoformat(waited[-1]["at"])


async def test_reject_redrafts_with_feedback_and_waits_again(api, service, gateway):
    run_id = await start(api, service)

    await api.post(f"/runs/{run_id}/resume", json={"decision": "reject", "comment": "更短一点"})
    await service.wait_idle()

    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "waiting"
    assert said(body["messages"][-2:]) == [
        ("user", "review", None, "选了「驳回」，意见：更短一点"),
        ("assistant", "draft", None, "修改后的回答"),
    ]
    second_prompt = gateway.requests[1]["body"]["messages"]
    assert "更短一点" in second_prompt[-1]["content"]


async def test_messages_from_before_steps_were_named_still_read(api, service):
    run_id = await start(api, service)
    record = await service._store.get(run_id)
    # A run saved before messages named their step: only role and content.
    graph = await service._graph(record)
    await graph.aupdate_state(
        _config(run_id), {"messages": [{"role": "user", "content": "旧消息"}]}
    )

    resp = await api.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["messages"][-1] == {
        "role": "user",
        "content": "旧消息",
        "node": None,
        "node_title": None,
        "actor": None,
        "at": None,
    }


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
    assert service.inputs(await service._store.create("etl", {"request": "x"})) is None
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


async def test_cancel_a_waiting_run(api, service):
    run_id = await start(api, service)

    resp = await api.post(f"/runs/{run_id}/cancel", json={"actor": "李雷", "reason": " 需求变了 "})
    assert resp.status_code == 202
    assert resp.json()["status"] == "cancelled"
    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "cancelled"
    assert body["cancelled_by"] == "李雷"
    assert body["error"] == "需求变了"
    assert body["pending"] is None
    assert body["finished_at"] == body["updated_at"]
    assert body["waiting_since"] is None and body["wait_seconds"] > 0
    assert body["actions"] == {"cancel": False, "retry": False, "rerun": True}
    flow = (await api.get(f"/runs/{run_id}/flow")).json()
    assert flow["steps"][-1]["node"] == "review"
    assert flow["steps"][-1]["status"] == "stopped"

    # A decision that comes too late, and a second cancel, change nothing.
    late = await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert late.status_code == 409
    again = await api.post(f"/runs/{run_id}/cancel", json={})
    assert again.status_code == 409
    assert (await api.get(f"/runs/{run_id}")).json()["status"] == "cancelled"
    assert (await api.post("/runs/nope/cancel", json={})).status_code == 404


async def test_cancel_stops_a_running_run_here_at_once(service, gateway):
    record = await service.create("hello", {"text": "你好"})
    # The run's task has not started its first node yet.
    await service.cancel(record.run_id, None, None)
    await service.wait_idle()

    stopped = await service._store.get(record.run_id)
    assert stopped.status == "cancelled"
    assert gateway.requests == []


async def test_a_run_cancelled_elsewhere_stops_before_its_next_node(api, service, gateway):
    record = await service.create("hello", {"text": "你好"})
    # Another process cancelled it: only the status says so, the task here runs on.
    await service._store.set_status(record.run_id, "cancelled", expected=("running",))
    await service.wait_idle()

    stopped = await service._store.get(record.run_id)
    assert stopped.status == "cancelled"
    assert gateway.requests == []
    # The step it stopped at shows no error of its own: the run says why it stopped.
    flow = (await api.get(f"/runs/{record.run_id}/flow")).json()
    assert flow["steps"] and flow["steps"][-1]["status"] == "stopped"
    assert all(step["error"] is None for step in flow["steps"])
    assert stopped.step["node"] == flow["current"]


async def test_a_run_cancelled_elsewhere_mid_step_says_where_it_stopped(api):
    store = MemoryRunStore()

    class CancelledMeanwhile(FakeGateway):
        def _handle(self, request):
            # While the model answers, another process cancels every run.
            for run_id, record in store._runs.items():
                store._runs[run_id] = replace(record, status="cancelled")
            return super()._handle(request)

    gateway = CancelledMeanwhile(["第一版回答"])
    service = RunService(
        store, InMemorySaver(), default_apps(), AppContext(gateway.client, SETTINGS)
    )
    app.state.run_service = service
    record = await service.create("hello", {"text": "你好"})
    await service.wait_idle()

    body = (await api.get(f"/runs/{record.run_id}")).json()
    flow = (await api.get(f"/runs/{record.run_id}/flow")).json()
    # It stopped before the confirmation: the list, the header and the flow say so.
    assert body["status"] == "cancelled"
    assert body["step"] == {"node": "review", "title": "确认"}
    assert flow["current"] == "review"
    assert [(s["node"], s["status"]) for s in flow["steps"]][-2:] == [
        ("draft", "done"),
        ("review", "stopped"),
    ]


async def test_of_two_decisions_sent_together_only_one_is_taken(service, gateway):
    record = await service.create("hello", {"text": "你好"})
    await service.wait_idle()

    outcomes = await asyncio.gather(
        service.resume(record.run_id, "reject", "更短一点"),
        service.resume(record.run_id, "approve", None),
        return_exceptions=True,
    )
    await service.wait_idle()
    assert sum(not isinstance(o, Exception) for o in outcomes) == 1
    assert sum(isinstance(o, NotWaiting) for o in outcomes) == 1
    assert len(gateway.requests) == (2 if isinstance(outcomes[1], NotWaiting) else 1)


async def test_a_cancel_sent_with_a_decision_wins_either_way(service):
    for order in ("cancel first", "decision first"):
        record = await service.create("hello", {"text": "你好"})
        await service.wait_idle()
        calls = [
            service.cancel(record.run_id, "李雷", None),
            service.resume(record.run_id, "approve", None),
        ]
        if order == "decision first":
            calls.reverse()
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        await service.wait_idle()
        assert not any(isinstance(o, NotCancellable) for o in outcomes), order
        assert (await service._store.get(record.run_id)).status == "cancelled", order


async def test_retry_a_run_that_failed_on_the_model(api, service, gateway):
    gateway.status = 500
    run_id = await start(api, service)
    failed = (await api.get(f"/runs/{run_id}")).json()
    assert failed["status"] == "error"
    assert failed["failed"] == {
        "node": "draft",
        "title": "回答",
        "error_type": "model_error",
        "retryable": True,
    }
    assert failed["actions"] == {"cancel": False, "retry": True, "rerun": True}

    # While a retry runs (here: in another process), the failed step runs again.
    await service._store.set_status(run_id, "running", expected=("error",))
    steps = (await api.get(f"/runs/{run_id}/flow")).json()["steps"]
    assert [(s["node"], s["status"], s["error"]) for s in steps][-1] == ("draft", "running", None)
    await service._store.set_status(run_id, "error", "调用模型失败", error_type="model_error")

    gateway.status = 200
    resp = await api.post(f"/runs/{run_id}/retry", json={"actor": "李雷"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    await service.wait_idle()
    body = (await api.get(f"/runs/{run_id}")).json()
    assert body["status"] == "waiting"
    assert body["error"] is None and body["failed"] is None
    assert said(body["messages"]) == [
        ("user", "start", None, "你好"),
        ("assistant", "draft", None, "第一版回答"),
    ]
    steps = (await api.get(f"/runs/{run_id}/flow")).json()["steps"]
    draft = [s for s in steps if s["node"] == "draft"][-1]
    assert (draft["status"], draft["error"]) == ("done", None)
    again = await api.post(f"/runs/{run_id}/retry", json={})
    assert again.status_code == 409
    assert again.json()["detail"] == "这次运行已经不在失败状态，可能有人已经重试过"


async def test_failures_that_would_happen_again_are_not_retried(api, service):
    record = await service._store.create("hello", {"text": "你好"})
    await service._store.set_status(record.run_id, "error", "格式不对", error_type="output_format")

    body = (await api.get(f"/runs/{record.run_id}")).json()
    assert body["failed"]["retryable"] is False
    assert body["actions"]["retry"] is False
    refused = await api.post(f"/runs/{record.run_id}/retry", json={})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "这次运行不能重试，请复制后重新发起"


async def test_cancel_retry_and_rerun_need_no_body(api, service, gateway):
    waiting = await start(api, service)
    cancelled = await api.post(f"/runs/{waiting}/cancel")
    assert (cancelled.status_code, cancelled.json()["status"]) == (202, "cancelled")
    copied = await api.post(f"/runs/{waiting}/rerun")
    assert copied.status_code == 201
    await service.wait_idle()

    gateway.status = 500
    failed = await start(api, service)
    gateway.status = 200
    retried = await api.post(f"/runs/{failed}/retry")
    assert (retried.status_code, retried.json()["status"]) == (202, "running")
    await service.wait_idle()
    assert (await api.get(f"/runs/{copied.json()['run_id']}")).json()["input"] == {"text": "你好"}


async def test_a_failure_waits_until_it_is_retried():
    store = MemoryRunStore()
    record = await store.create("hello", {"text": "你好"})
    await store.set_status(record.run_id, "error", "模型出错", error_type="model_error")
    failed = await store.get(record.run_id)
    store._runs[record.run_id] = replace(failed, updated_at=failed.updated_at - timedelta(hours=5))

    await store.set_status(record.run_id, "running", expected=("error",))
    # The five hours nobody retried it are not the machine's.
    assert (await store.get(record.run_id)).wait_seconds >= 5 * 3600


async def test_retry_before_the_first_step_starts_from_the_input(service):
    # It failed before anything was saved (say, its configuration could not be read).
    record = await service._store.create("hello", {"text": "你好"}, created_by="李雷")
    await service._store.set_status(record.run_id, "error", "启动失败")

    await service.retry(record.run_id)
    await service.wait_idle()
    view = await service.get(record.run_id)
    assert view.record.status == "waiting"
    assert said(view.messages)[0] == ("user", "start", "李雷", "你好")


async def test_a_run_recovered_before_its_first_step_names_its_requester(service):
    record = await service._store.create("hello", {"text": "你好"}, created_by="李雷")

    assert await service.recover() == 1
    await service.wait_idle()
    assert said((await service.get(record.run_id)).messages)[0] == ("user", "start", "李雷", "你好")


async def test_rerun_copies_the_version_and_input(api, service):
    run_id = await start(api, service, text="原来的需求")
    source = (await api.get(f"/runs/{run_id}")).json()

    same = await api.post(f"/runs/{run_id}/rerun", json={"requester": "韩梅梅"})
    assert same.status_code == 201
    changed = await api.post(f"/runs/{run_id}/rerun", json={"input": {"text": "改过的需求"}})
    await service.wait_idle()
    assert source["inputs"] == [
        {"key": "text", "label": "问题", "type": "textarea", "required": True}
    ]
    for resp, text, requester in ((same, "原来的需求", "韩梅梅"), (changed, "改过的需求", None)):
        body = (await api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert body["rerun_of"] == run_id
        assert body["app_version"] == source["app_version"]
        assert body["input"] == {"text": text}
        assert body["title"] == text
        assert body["created_by"] == requester

    bad = await api.post(f"/runs/{run_id}/rerun", json={"input": {"text": ""}})
    assert bad.status_code == 422
    assert (await api.post("/runs/nope/rerun", json={})).status_code == 404


async def test_a_run_from_before_versions_is_copied_into_the_current_version(api, service):
    record = await service._store.create("hello", {"text": "你好"})
    config = copy.deepcopy(default_apps()["hello"].config)
    audience = {"key": "audience", "label": "给谁看", "type": "text", "required": True}
    config["graph"]["nodes"][0]["inputs"].append(audience)
    await service.ensure_versions("hello")
    await service.versions.save_draft("hello", config, None)
    await service.versions.publish("hello", "加了给谁看", None)

    body = (await api.get(f"/runs/{record.run_id}")).json()
    assert [field["key"] for field in body["inputs"]] == ["text", "audience"]
    copied = await api.post(
        f"/runs/{record.run_id}/rerun", json={"input": {"text": "你好", "audience": "老板"}}
    )
    assert copied.status_code == 201


async def test_rerun_of_a_draft_run_uses_the_configuration_it_ran_with(api, service):
    config = copy.deepcopy(default_apps()["hello"].config)
    config["graph"]["nodes"][1]["title"] = "草稿里的回答"
    audience = {"key": "audience", "label": "给谁看", "type": "text", "required": True}
    config["graph"]["nodes"][0]["inputs"].append(audience)
    record = await service.create("hello", {"text": "你好", "audience": "老板"}, config=config)
    await service.wait_idle()
    # A copy asks for what the draft asked for, not what the app asks for now.
    body = (await api.get(f"/runs/{record.run_id}")).json()
    assert [field["key"] for field in body["inputs"]] == ["text", "audience"]

    again = await service.rerun(record.run_id)
    await service.wait_idle()
    assert again.app_version is None
    assert again.config == record.config
    assert (await service._store.get(again.run_id)).step == {"node": "review", "title": "确认"}
