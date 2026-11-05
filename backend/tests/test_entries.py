"""The ways into an app, each of which can be closed (docs/api/app-access.md, "入口"), and
how an app is used (docs/api/ops.md, "应用的使用情况")."""

from typing import Any

from aep import entries as ways
from aep.feedback import Feedback, MemoryFeedbackStore
from aep.main import app as api_app
from aep.models import MemoryCallStore, MemorySettingStore
from aep.ops import MemoryBudgetStore, MemoryComputeStore, Ops
from aep.runtime.store import RunRecord
from tests.test_agent_apps import data_agent, new_agent, publish
from tests.test_app_access import Access, bearer
from tests.test_auth import signed
from tests.test_conversations import chat_app, llm_chat, open_chat, say
from tests.test_flow_app_node import LOOKUP, call, caller, save
from tests.test_flow_tools import Platform, answer
from tests.test_flow_tools import call as tool_call

ALL_OPEN = {"use": True, "api": True, "mcp": True, "apps": True}


def switches(access: dict[str, Any]) -> dict[str, bool]:
    return {entry: access[entry]["enabled"] for entry in ways.ENTRIES}


async def test_entries_are_open_until_closed_and_say_when_they_were_last_used():
    p = Access()
    await chat_app(p, "chat", llm_chat())
    secret = await p.key("chat")
    async with p.client() as api:
        got = await api.get("/apps/chat/access")
        assert got.status_code == 200, got.text
        assert switches(got.json()) == ALL_OPEN
        assert all(got.json()[e]["last_used_at"] is None for e in ways.ENTRIES)

        assert (
            await api.post("/runs", json={"app": "chat", "input": {"message": "你好"}})
        ).is_success
        ran = await api.post(
            "/v1/apps/chat/runs", json={"input": {"message": "你好"}}, headers=bearer(secret)
        )
        assert ran.status_code == 201, ran.text
        await p.service.wait_idle()
        used = (await api.get("/apps/chat/access")).json()
        assert used["use"]["last_used_at"] and used["api"]["last_used_at"]
        assert used["mcp"]["last_used_at"] is None and used["apps"]["last_used_at"] is None

        changed = await api.put("/apps/chat/access", json={"mcp": False, "apps": False})
        assert changed.status_code == 200, changed.text
        assert switches(changed.json()) == {**ALL_OPEN, "mcp": False, "apps": False}
        assert switches((await api.get("/apps/chat/access")).json())["mcp"] is False
        assert (await api.put("/apps/chat/access", json={"nope": True})).status_code == 422
        assert (await api.get("/apps/nobody/access")).status_code == 404
        assert (await api.put("/apps/nobody/access", json={"use": False})).status_code == 404


async def test_a_closed_api_or_mcp_entry_refuses_its_callers():
    p = Access()
    await chat_app(p, "chat", llm_chat())
    secret = await p.key("chat")
    async with p.client() as api:
        await api.put("/apps/chat/access", json={"api": False, "mcp": False})
        for body in (
            {"input": {"message": "你好"}},
            {"input": {"message": "你好"}, "new_conversation": True},
        ):
            refused = await api.post("/v1/apps/chat/runs", json=body, headers=bearer(secret))
            assert refused.status_code == 403
            assert refused.json()["detail"] == "这个应用没有开放这个入口"
        refused = await api.post(
            "/mcp/apps/chat",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**bearer(secret), "Accept": "application/json, text/event-stream"},
        )
        assert refused.status_code == 403
        assert refused.json()["detail"] == "这个应用没有开放这个入口"
        # The keys stay: open again, and they work.
        await api.put("/apps/chat/access", json={"api": True})
        ran = await api.post(
            "/v1/apps/chat/runs", json={"input": {"message": "你好"}}, headers=bearer(secret)
        )
        assert ran.status_code == 201, ran.text


async def test_an_app_closed_to_users_still_runs_for_builders():
    p = Access(enabled=True)
    await p.user("builder", ["builder"], "王搭建")
    await p.user("lilei", ["user"], "李雷")
    async with signed(await p.signed_in("builder")) as api:
        for method, path, body in (
            ("POST", "/apps", {"app": "chat", "title": "报销助手", "from": {"blank": True}}),
            ("PUT", "/apps/chat/draft", {"config": llm_chat()}),
            ("POST", "/apps/chat/draft/publish", {"note": "测试"}),
        ):
            resp = await api.request(method, path, json=body)
            assert resp.is_success, resp.text
    async with signed(await p.signed_in("lilei")) as api:
        opened = await api.post("/conversations", json={"app": "chat"})
        assert opened.status_code == 201, opened.text
        cid = opened.json()["conversation_id"]
        assert (await api.put("/apps/chat/access", json={"use": False})).status_code == 403
    async with signed(await p.signed_in("builder")) as api:
        assert (await api.put("/apps/chat/access", json={"use": False})).status_code == 200
        ran = await api.post("/runs", json={"app": "chat", "input": {"message": "试一下"}})
        assert ran.status_code == 201, ran.text
        assert "chat" in [a["app"] for a in (await api.get("/apps")).json()]
    async with signed(await p.signed_in("lilei")) as api:
        for path, body in (
            ("/runs", {"app": "chat", "input": {"message": "你好"}}),
            ("/conversations", {"app": "chat"}),
            (f"/conversations/{cid}/messages", {"message": "你好"}),
        ):
            refused = await api.post(path, json=body)
            assert refused.status_code == 403, path
            assert refused.json()["detail"] == "这个应用没有开放给使用者"
        assert "chat" not in [a["app"] for a in (await api.get("/apps")).json()]
    await p.service.wait_idle()
    labels = [e["label"] for e in p.store.audit]
    assert "修改应用入口" in labels


async def test_an_app_closed_to_other_apps_cannot_be_called():
    async with Platform(["订单 42 已发货"]) as p:
        await p.new_app("lookup", LOOKUP)
        await p.new_app("front", caller(call()))
        called = (await p.api.get("/apps/lookup/access")).json()["apps"]
        assert [(c["app"], c["via"]) for c in called["callers"]] == [("front", "node")]
        await p.api.put("/apps/lookup/access", json={"apps": False})
        refused = await save(p, "other", caller(call()))
        assert refused.status_code == 422
        assert [d["msg"] for d in refused.json()["detail"]] == [
            "应用「lookup」不允许被其他应用调用"
        ]
        # A caller published before fails at the node, as an input problem.
        run = await p.run("front", {"question": "42"})
        assert run["status"] == "error"
        assert run["failed"]["error_type"] == "input_error"
        assert "应用「lookup」不允许被其他应用调用" in run["error"]


def test_runs_from_before_entries_are_told_by_how_they_started():
    def record(**changes: Any) -> RunRecord:
        return RunRecord("r", "chat", "done", {}, **changes)

    assert ways.of(record(entry="mcp")) == "mcp"
    assert ways.of(record(trigger={"kind": "schedule"})) == "schedule"
    assert ways.of(record(trigger={"kind": "app", "run_id": "x"})) == "apps"
    assert ways.of(record(start_at={"node": "a"})) == "other"
    assert ways.of(record(created_by="aep-eval")) == "other"
    assert ways.of(record(created_by="API：CRM 系统")) == "api"
    assert ways.of(record(created_by="李雷")) == "use"


# Usage


def watched(p: Platform) -> MemoryFeedbackStore:
    """The usage endpoint's view of the platform: its runs, calls and feedback."""
    api_app.state.ops = Ops(
        p.service._store,  # noqa: SLF001
        MemoryCallStore(),
        MemoryComputeStore(),
        MemoryBudgetStore(),
        MemorySettingStore(),
        p.service.app_titles,
    )
    feedback = MemoryFeedbackStore()
    api_app.state.feedback = feedback
    return feedback


async def test_usage_counts_runs_by_entry_with_conversations_answers_and_tools():
    async with Platform([]) as p:
        feedback = watched(p)
        await new_agent(p, "ask-data", {"template": "agent-data"})
        await publish(p, "ask-data", data_agent(p.shop))
        p.gateway.replies = [
            tool_call("t4", schema="sales", table="customers"),
            answer("有 3 个客户"),
            answer("大概有一百个客户吧"),
            tool_call("t4", schema="sales", table="nope"),
            answer("查不到这张表"),
            answer("草稿试一下"),
        ]
        cid = await open_chat(p, "ask-data")
        first = await say(p, cid, "有多少客户？")
        await say(p, cid, "你确定吗？")
        once = await p.run("ask-data", {"message": "有张表叫 nope 吗？"})
        assert first["result"]["grounded"] is True and once["status"] == "done"

        await feedback.put(Feedback(first["run_id"], "", "good"))
        scheduled = await p.service.create(
            "ask-data",
            {"message": "定时问一下"},
            trigger={"kind": "schedule", "schedule_id": "s1", "title": "每天"},
        )
        await p.service.wait_idle()
        assert scheduled.entry == "schedule"
        # A draft's trial run is not use, as on the dashboard.
        saved = await p.api.put("/apps/ask-data/draft", json={"config": data_agent(p.shop)})
        assert saved.status_code == 200, saved.text
        tried = await p.api.post(
            "/runs", json={"app": "ask-data", "input": {"message": "试一下"}, "draft": True}
        )
        assert tried.status_code == 201, tried.text
        await p.service.wait_idle()

        got = await p.api.get("/apps/ask-data/usage", params={"days": 7})
        assert got.status_code == 200, got.text
        usage = got.json()
        assert usage["days"] == 7 and usage["truncated"] is False
        assert usage["totals"]["runs"] == 4
        assert sum(day["runs"] for day in usage["daily"]) == 4 and len(usage["daily"]) == 7
        assert usage["entries"] == {
            "use": 3,
            "api": 0,
            "mcp": 0,
            "apps": 0,
            "schedule": 1,
            "other": 0,
        }
        assert usage["conversations"] == {"count": 1, "turns_avg": 2.0}
        # Only the first turn stood on the data.
        assert usage["answers"] == {"answered": 1, "ungrounded": 3}
        assert usage["feedback"] == {"good": 1, "bad": 0}
        [sampled] = usage["tools"]
        assert sampled["tool"] == "sample_rows" and sampled["datasource"] == p.shop
        assert (sampled["calls"], sampled["errors"], sampled["app"]) == (2, 1, None)
        assert usage["apps"] == []

        assert (await p.api.get("/apps/ask-data/usage", params={"days": 3})).status_code == 422
        assert (await p.api.get("/apps/nobody/usage")).status_code == 404


async def test_usage_lists_the_apps_an_app_called():
    async with Platform(["订单 42 已发货", "订单 7 已签收"]) as p:
        watched(p)
        await p.new_app("lookup", LOOKUP)
        await p.new_app("front", caller(call()))
        await p.run("front", {"question": "42"})
        await p.run("front", {"question": "7"})
        usage = (await p.api.get("/apps/front/usage")).json()
        assert usage["apps"] == [{"app": "lookup", "title": "lookup", "calls": 2, "errors": 0}]
        assert usage["conversations"] is None and usage["answers"] is None
        called = (await p.api.get("/apps/lookup/usage")).json()
        assert called["entries"]["apps"] == 2


async def test_only_builders_see_usage():
    p = Access(enabled=True)
    await p.user("lilei", ["user"], "李雷")
    async with signed(await p.signed_in("lilei")) as api:
        assert (await api.get("/apps/hello/usage")).status_code == 403
