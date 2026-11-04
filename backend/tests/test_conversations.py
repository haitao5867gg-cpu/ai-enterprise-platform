"""Conversations: chatting with an app turn by turn (docs/api/conversations.md)."""

import asyncio
import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aep import conversations as chat
from aep import policy
from aep.policy import DataPolicy
from aep.runtime.flow.check import check_config
from aep.runtime.service import retryable
from aep.runtime.store import MemoryRunStore, RunRecord, TurnTaken
from tests.test_app_access import Access, bearer, mcp_client
from tests.test_auth import Platform as Accounts
from tests.test_auth import signed
from tests.test_flow_apps import ENV, problems
from tests.test_flow_tools import Platform, answer, call
from tests.test_policy import with_policy

MESSAGE = {"key": "message", "label": "消息", "type": "textarea", "required": True}
EMAIL = "a@example.com"  # the first customer's, in the fake data source


def start(*inputs: dict) -> dict:
    return {"id": "start", "type": "start", "title": "开始", "inputs": [dict(MESSAGE), *inputs]}


def reply_end(source: str) -> dict:
    return {
        "id": "end",
        "type": "end",
        "title": "结束",
        "outputs": [{"key": "reply", "from": [source]}],
    }


def in_a_row(*nodes: dict, conversation: dict | None = None) -> dict:
    edges = [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:], strict=False)]
    config: dict[str, Any] = {"graph": {"nodes": list(nodes), "edges": edges}}
    if conversation is not None:
        config["conversation"] = conversation
    return config


def llm_chat(prompt: str = "{{start.message}}", history: bool = True, **settings: Any) -> dict:
    """An app that answers each message with one model call."""
    ask = {"id": "ask", "type": "llm", "title": "回答", "prompt": prompt, "history": history}
    return in_a_row(
        start(), ask, reply_end("{{ask.text}}"), conversation={"reply": "reply", **settings}
    )


def form_chat() -> dict:
    """An app whose every turn waits for a person to fill in a form."""
    ask = {
        "id": "ask",
        "type": "human",
        "title": "补充",
        "mode": "form",
        "prompt": "请补充订单号",
        "fields": [{"key": "order", "label": "订单号", "type": "text", "required": True}],
    }
    return in_a_row(start(), ask, reply_end("{{ask.order}}"), conversation={"reply": "reply"})


def agent_chat() -> dict:
    """An agent that may read a data source chosen when the conversation opens."""
    datasource = {"key": "datasource", "label": "数据源", "type": "datasource", "required": True}
    agent = {
        "id": "agent",
        "type": "agent",
        "title": "查数据并回答",
        "task": "{{start.message}}",
        "history": True,
        "tools": [{"datasource": "{{start.datasource}}", "tools": None}],
        "max_steps": 8,
    }
    return in_a_row(
        start(datasource), agent, reply_end("{{agent.text}}"), conversation={"reply": "reply"}
    )


async def open_chat(p: Any, app: str, **body: Any) -> str:
    resp = await p.api.post("/conversations", json={"app": app, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()["conversation_id"]


async def say(p: Any, conversation_id: str, message: str) -> dict:
    """Send a message, wait for its turn and return the turn's run."""
    resp = await p.api.post(f"/conversations/{conversation_id}/messages", json={"message": message})
    assert resp.status_code == 201, resp.text
    await p.service.wait_idle()
    return (await p.api.get(f"/runs/{resp.json()['run_id']}")).json()


def sent(p: Any, index: int) -> list[tuple[str, str]]:
    """The messages of a model request, without its system prompt."""
    messages = p.gateway.requests[index]["body"]["messages"]
    return [(m["role"], m["content"]) for m in messages if m["role"] != "system"]


def agent_said(text: str) -> str:
    return json.dumps({"action": "answer", "text": text}, ensure_ascii=False)


# Turns and their history


async def test_each_message_is_a_turn_and_later_turns_get_the_earlier_ones():
    async with Platform(["一线城市 600 元", "二线城市 450 元", "三线城市 350 元"]) as p:
        await p.new_app("chat", llm_chat())
        opened = await p.api.post("/conversations", json={"app": "chat"})
        assert opened.status_code == 201, opened.text
        found = opened.json()
        cid = found["conversation_id"]
        assert cid.startswith("conv_") and found["app"] == "chat" and found["app_title"] == "chat"
        assert found["title"] == "新对话" and found["input"] == {} and found["draft"] is False
        assert found["turn_count"] == 0 and found["busy"] is False
        updated = datetime.fromisoformat(found["updated_at"])
        assert datetime.fromisoformat(found["expires_at"]) == updated + timedelta(days=30)

        first = await say(p, cid, "出差住宿标准是多少？")
        assert first["status"] == "done", first["error"]
        assert first["result"] == {"reply": "一线城市 600 元"}
        assert first["title"] == "出差住宿标准是多少？" and first["cleared"] is False
        assert first["conversation"] == {
            "conversation_id": cid,
            "title": "出差住宿标准是多少？",
            "turn": 1,
        }
        await say(p, cid, "那二线城市呢？")
        third = await say(p, cid, "三线呢？")
        assert third["conversation"]["turn"] == 3

        assert sent(p, 0) == [("user", "出差住宿标准是多少？")]
        assert sent(p, 2) == [
            ("user", "出差住宿标准是多少？"),
            ("assistant", "一线城市 600 元"),
            ("user", "那二线城市呢？"),
            ("assistant", "二线城市 450 元"),
            ("user", "三线呢？"),
        ]

        got = (await p.api.get(f"/conversations/{cid}")).json()
        assert got["title"] == "出差住宿标准是多少？"
        assert got["turn_count"] == 3 and got["busy"] is False
        assert [(t["turn"], t["message"], t["reply"], t["status"]) for t in got["turns"]] == [
            (1, "出差住宿标准是多少？", "一线城市 600 元", "done"),
            (2, "那二线城市呢？", "二线城市 450 元", "done"),
            (3, "三线呢？", "三线城市 350 元", "done"),
        ]
        assert all(t["finished_at"] and t["pending"] is None for t in got["turns"])
        assert datetime.fromisoformat(got["updated_at"]) > updated
        listed = (await p.api.get("/conversations", params={"app": "chat"})).json()
        assert [(c["conversation_id"], c["turn_count"]) for c in listed] == [(cid, 3)]
        assert (await p.api.get("/conversations", params={"app": "other"})).json() == []

        # The run page shows the history the turn's models got.
        flow = (await p.api.get(f"/runs/{third['run_id']}/flow")).json()
        step = next(s for s in flow["steps"] if s["node"] == "ask")
        assert step["input"]["conversation"] == {
            "id": cid,
            "turn": 3,
            "history": [
                {"message": "出差住宿标准是多少？", "reply": "一线城市 600 元"},
                {"message": "那二线城市呢？", "reply": "二线城市 450 元"},
            ],
        }
        runs = (await p.api.get("/runs")).json()
        assert sorted(r["conversation"]["turn"] for r in runs) == [1, 2, 3]


async def test_an_agent_gets_earlier_answers_but_not_its_tool_results():
    async with Platform([]) as p:
        await p.new_app("ask-data", agent_chat())
        cid = await open_chat(p, "ask-data", input={"datasource": p.shop})
        p.gateway.replies = [
            call("t4", schema="sales", table="customers"),
            answer(f"第一个客户的邮箱是 {EMAIL}"),
        ]
        first = await say(p, cid, "第一个客户是谁？")
        assert first["status"] == "done", first["error"]
        assert first["input"] == {"message": "第一个客户是谁？", "datasource": p.shop}
        assert EMAIL in p.gateway.requests[1]["body"]["messages"][-1]["content"]

        p.gateway.replies = [answer("上一位是第一个客户")]
        second = await say(p, cid, "刚才说的是谁？")
        assert second["result"] == {"reply": "上一位是第一个客户"}
        assert sent(p, 2) == [
            ("user", "第一个客户是谁？"),
            ("assistant", agent_said(f"第一个客户的邮箱是 {EMAIL}")),
            ("user", "刚才说的是谁？"),
        ]


async def test_prompts_can_name_the_conversation():
    prompt = "第 {{conversation.turn}} 轮\n{{conversation.history}}\n问题：{{start.message}}"
    async with Platform(["答一", "答二", "答三"]) as p:
        await p.new_app("chat", llm_chat(prompt, history=False))
        cid = await open_chat(p, "chat")
        await say(p, cid, "一")
        await say(p, cid, "二")
        assert sent(p, 0) == [("user", "第 1 轮\n\n问题：一")]
        assert sent(p, 1) == [("user", "第 2 轮\n用户：一\n助手：答一\n问题：二")]

        # Started on its own the app runs as it always did: no turn, no history.
        run = await p.run("chat", {"message": "三"})
        assert run["status"] == "done" and run["conversation"] is None
        assert run["cleared"] is False
        assert sent(p, 2) == [("user", "第 0 轮\n\n问题：三")]


async def test_only_done_turns_within_the_limits_are_history():
    async with Platform(["答一", "答二", "答三"]) as p:
        await p.new_app("chat", llm_chat(history_turns=1))
        cid = await open_chat(p, "chat")
        await say(p, cid, "一")
        await say(p, cid, "二")
        await say(p, cid, "三")
        assert sent(p, 2) == [("user", "二"), ("assistant", "答二"), ("user", "三")]


def record(turn: int, message: str, status: str = "done", cleared: bool = False) -> RunRecord:
    return RunRecord(
        f"run-{turn}",
        "chat",
        status,  # type: ignore[arg-type]
        {"message": message},
        conversation_id="conv_1",
        conversation_turn=turn,
        cleared_at=datetime.now(UTC) if cleared else None,
    )


def reply(
    turn: int,
    text: str,
    samples: str = "full",
    for_model: str | None = None,
    restored: dict[str, str] | None = None,
) -> chat.Reply:
    return chat.Reply(f"run-{turn}", "conv_1", text, for_model or text, restored or {}, samples)


def test_the_history_keeps_whole_done_turns_within_the_limits():
    turns = [
        (record(1, "一"), reply(1, "答一")),
        (record(2, "二", "error"), None),
        (record(3, "三", "cancelled"), None),
        (record(4, "四"), reply(4, "答四")),
        (record(5, "五", cleared=True), reply(5, "答五")),
        (record(6, "六", "running"), None),
    ]
    found, _ = chat.history(turns, "full", 10)
    assert found == [{"message": "一", "reply": "答一"}, {"message": "四", "reply": "答四"}]
    assert chat.history(turns, "full", 1)[0] == [{"message": "四", "reply": "答四"}]
    assert chat.history(turns, "full", 0)[0] == []

    # Over 20000 characters, whole turns go from the oldest.
    long = [
        (record(1, "长" * 15000), reply(1, "答")),
        (record(2, "短"), reply(2, "答" * 5000)),
        (record(3, "三"), reply(3, "答三")),
    ]
    assert [t["message"] for t in chat.history(long, "full", 10)[0]] == ["短", "三"]
    # A turn whose reply was not kept (or was no text) goes with its message only.
    assert chat.history([(record(1, "一"), None)], "masked", 10)[0] == [
        {"message": "一", "reply": None}
    ]


def test_the_history_follows_the_samples_level():
    token = policy.token("conv_1", EMAIL)
    restored = {token: EMAIL}
    masked = (record(1, "一"), reply(1, f"邮箱 {EMAIL}", "masked", f"邮箱 {token}", restored))
    full = (record(2, "二"), reply(2, "答二", "full"))

    found, tokens = chat.history([masked], "masked", 10)
    assert found == [{"message": "一", "reply": f"邮箱 {token}"}] and tokens == restored
    # Values may go to models now: the reply goes as people saw it.
    assert chat.history([masked], "full", 10)[0] == [{"message": "一", "reply": f"邮箱 {EMAIL}"}]
    # The full turn's model saw values this turn's may not: it stays out.
    found, tokens = chat.history([masked, full], "masked", 10)
    assert [t["message"] for t in found] == ["一"] and tokens == restored
    assert chat.history([masked, full], "schema", 10) == ([], {})
    # The tokens of turns left out stay out too.
    later = (record(3, "三"), reply(3, "答三", "masked"))
    assert chat.history([masked, later], "masked", 1) == ([{"message": "三", "reply": "答三"}], {})


async def test_masked_history_carries_tokens_that_turn_back_in_later_turns():
    async with Platform([], settings=with_policy(data_samples="masked")) as p:
        await p.new_app("ask-data", agent_chat())
        cid = await open_chat(p, "ask-data", input={"datasource": p.shop})
        token = policy.token(cid, EMAIL)  # tokens are the conversation's, not the run's
        p.gateway.replies = [
            call("t4", schema="sales", table="customers"),
            answer(f"第一个客户的邮箱是 {token}"),
        ]
        first = await say(p, cid, "第一个客户是谁？")
        assert first["status"] == "done", first["error"]
        assert first["policy"]["samples"] == "masked"
        assert first["result"] == {"reply": f"第一个客户的邮箱是 {EMAIL}"}
        result = p.gateway.requests[1]["body"]["messages"][-1]["content"]
        assert token in result and EMAIL not in result

        # This turn reads no data: the token turns back with what the first turn restored.
        p.gateway.replies = [answer(f"他的邮箱是 {token}")]
        second = await say(p, cid, "他的邮箱再说一遍")
        assert second["status"] == "done", second["error"]
        assert second["result"] == {"reply": f"他的邮箱是 {EMAIL}"}
        assert sent(p, 2) == [
            ("user", "第一个客户是谁？"),
            ("assistant", agent_said(f"第一个客户的邮箱是 {token}")),
            ("user", "他的邮箱再说一遍"),
        ]
        assert EMAIL not in json.dumps(p.gateway.requests[2]["body"], ensure_ascii=False)

        # People see the values; the run page shows the history as the model got it.
        got = (await p.api.get(f"/conversations/{cid}")).json()
        assert [t["reply"] for t in got["turns"]] == [
            f"第一个客户的邮箱是 {EMAIL}",
            f"他的邮箱是 {EMAIL}",
        ]
        flow = (await p.api.get(f"/runs/{second['run_id']}/flow")).json()
        step = next(s for s in flow["steps"] if s["node"] == "agent")
        assert step["input"]["conversation"]["history"] == [
            {"message": "第一个客户是谁？", "reply": f"第一个客户的邮箱是 {token}"}
        ]


async def test_a_turn_run_at_a_looser_level_stays_out_of_the_history(monkeypatch):
    async with Platform(["答一", "答二"]) as p:
        await p.new_app("chat", llm_chat())
        cid = await open_chat(p, "chat")
        first = await say(p, cid, "一")
        assert first["policy"]["samples"] == "full"
        monkeypatch.setattr(policy, "platform_default", lambda settings: DataPolicy("masked"))
        second = await say(p, cid, "二")
        assert second["policy"]["samples"] == "masked"
        assert sent(p, 1) == [("user", "二")]


# Checking the settings


async def test_the_conversation_settings_are_checked():
    kept = await check_config(llm_chat(), ENV)
    assert kept["conversation"] == {"enabled": True, "history_turns": 10, "reply": "reply"}

    plain = llm_chat()
    del plain["conversation"]
    assert await problems(plain) == {
        ("config", "graph", "nodes", "ask", "history"): "应用没有打开对话，不能带会话历史"
    }
    named = llm_chat("{{conversation.history}}", history=False)
    del named["conversation"]
    assert await problems(named) == {
        ("config", "graph", "nodes", "ask", "prompt"): "应用没有打开对话，不能引用会话"
    }
    off = llm_chat(enabled=False)
    assert await problems(off) == {
        ("config", "graph", "nodes", "ask", "history"): "应用没有打开对话，不能带会话历史"
    }
    assert await problems(llm_chat("{{conversation.title}}")) == {
        ("config", "graph", "nodes", "ask", "prompt"): "会话没有 title 这一项，可以用 history、turn"
    }
    assert await problems(llm_chat(reply="answer")) == {
        ("config", "conversation", "reply"): "结束节点没有输出 answer"
    }
    assert await problems(llm_chat(reply=None)) == {
        ("config", "conversation", "reply"): "要选一个结束节点的输出作为回答"
    }
    for turns in (51, -1, "10", True):
        assert await problems(llm_chat(history_turns=turns)) == {
            ("config", "conversation", "history_turns"): "带的历史轮数要在 0–50 之间"
        }
    assert await problems(llm_chat(history="yes")) == {  # type: ignore[arg-type]
        ("config", "graph", "nodes", "ask", "history"): "history 必须是 true 或 false"
    }
    for field in ({"type": "number"}, {"required": False}, {"key": "question"}):
        config = llm_chat("{{start.question}}" if "key" in field else "{{start.message}}")
        config["graph"]["nodes"][0]["inputs"][0] |= field
        assert await problems(config) == {
            (
                "config",
                "graph",
                "nodes",
                "start",
                "inputs",
            ): "打开对话要在开始节点里有一个必填的文字输入 message"
        }


# The API


async def test_opening_and_sending_are_checked():
    async with Platform(["答"]) as p:
        await p.new_app("chat", llm_chat())
        await p.new_app("ask-data", agent_chat())
        missing = await p.api.post("/conversations", json={"app": "nope"})
        assert missing.status_code == 404 and missing.json()["detail"] == "没有应用 nope"
        for app in ("hello", "etl"):
            refused = await p.api.post("/conversations", json={"app": app})
            assert refused.status_code == 409 and refused.json()["detail"] == "这个应用没有开对话"
        no_source = await p.api.post("/conversations", json={"app": "ask-data"})
        assert no_source.status_code == 422
        assert no_source.json()["detail"][0]["loc"] == ["body", "input", "datasource"]
        early = await p.api.post("/conversations", json={"app": "chat", "input": {"message": "a"}})
        assert early.status_code == 422
        assert early.json()["detail"][0]["loc"] == ["body", "input", "message"]

        cid = await open_chat(p, "chat", title="我的会话")
        for message in ("   ", "长" * 8001):
            bad = await p.api.post(f"/conversations/{cid}/messages", json={"message": message})
            assert bad.status_code == 422
        await say(p, cid, "出差住宿标准是多少？")
        got = (await p.api.get(f"/conversations/{cid}")).json()
        assert got["title"] == "我的会话"  # a name someone gave stays

        renamed = await p.api.patch(f"/conversations/{cid}", json={"title": " 住宿 "})
        assert renamed.status_code == 200 and renamed.json()["title"] == "住宿"
        blank = await p.api.patch(f"/conversations/{cid}", json={"title": " "})
        assert blank.status_code == 422
        long_title = "这是一个很长的问题" * 5
        other = await open_chat(p, "chat")
        await say(p, other, long_title)
        named = (await p.api.get(f"/conversations/{other}")).json()
        assert named["title"] == long_title[:30]

        unknown = await p.api.get("/conversations/conv_nope")
        assert unknown.status_code == 404 and unknown.json()["detail"] == "没有找到这个会话"
        listed = (await p.api.get("/conversations")).json()
        assert [c["conversation_id"] for c in listed] == [other, cid]


async def test_a_turn_waiting_for_a_person_holds_the_next_message():
    async with Platform([]) as p:
        await p.new_app("orders", form_chat())
        cid = await open_chat(p, "orders")
        first = await say(p, cid, "补录一单")
        assert first["status"] == "waiting"
        got = (await p.api.get(f"/conversations/{cid}")).json()
        assert got["busy"] is True
        assert got["turns"][0]["pending"]["kind"] == "form"
        assert got["turns"][0]["reply"] is None and got["turns"][0]["finished_at"] is None
        busy = await p.api.post(f"/conversations/{cid}/messages", json={"message": "再补一单"})
        assert busy.status_code == 409 and busy.json()["detail"] == "上一条还没处理完"

        resumed = await p.api.post(
            f"/runs/{first['run_id']}/resume",
            json={"decision": "submit", "values": {"order": "SO-1"}},
        )
        assert resumed.status_code == 202, resumed.text
        await p.service.wait_idle()
        got = (await p.api.get(f"/conversations/{cid}")).json()
        assert got["busy"] is False and got["turns"][0]["reply"] == "SO-1"
        second = await say(p, cid, "再补一单")
        assert second["status"] == "waiting" and second["conversation"]["turn"] == 2


async def test_two_messages_at_once_start_one_turn():
    async with Platform([]) as p:
        await p.new_app("orders", form_chat())
        cid = await open_chat(p, "orders")
        path = f"/conversations/{cid}/messages"
        a, b = await asyncio.gather(
            p.api.post(path, json={"message": "一"}), p.api.post(path, json={"message": "二"})
        )
        assert sorted([a.status_code, b.status_code]) == [201, 409]
        await p.service.wait_idle()


async def test_a_turn_is_taken_once_in_the_store():
    store = MemoryRunStore()
    first = await store.create("chat", {}, conversation_id="conv_1", conversation_turn=1)
    with pytest.raises(TurnTaken):
        await store.create("chat", {}, conversation_id="conv_1", conversation_turn=1)
    second = await store.create("chat", {}, conversation_id="conv_1", conversation_turn=2)
    await store.create("chat", {}, conversation_id="conv_2", conversation_turn=1)
    found = await store.in_conversation("conv_1")
    assert [r.run_id for r in found] == [first.run_id, second.run_id]


async def test_a_draft_conversation_runs_the_draft():
    async with Platform(["草稿的回答"]) as p:
        published = llm_chat(history=False)
        del published["conversation"]
        await p.new_app("chat", published)
        refused = await p.api.post("/conversations", json={"app": "chat"})
        assert refused.status_code == 409
        no_draft = await p.api.post("/conversations", json={"app": "chat", "draft": True})
        assert no_draft.status_code == 409 and no_draft.json()["detail"] == "没有草稿可以试"

        saved = await p.api.put("/apps/chat/draft", json={"config": llm_chat()})
        assert saved.status_code == 200, saved.text
        cid = await open_chat(p, "chat", draft=True)
        turn = await say(p, cid, "试一下")
        assert turn["status"] == "done" and turn["app_version"] is None
        assert turn["result"] == {"reply": "草稿的回答"}
        assert (await p.api.get("/conversations")).json() == []  # drafts are listed apart
        drafts = (await p.api.get("/conversations", params={"draft": True})).json()
        assert [c["conversation_id"] for c in drafts] == [cid]

        assert (await p.api.delete("/apps/chat/draft")).status_code == 204
        gone = await p.api.post(f"/conversations/{cid}/messages", json={"message": "再试"})
        assert gone.status_code == 409 and gone.json()["detail"] == "没有草稿可以试"


async def test_only_the_owner_and_admins_see_a_conversation():
    p = Accounts()
    await p.user("builder", ["builder"], "王搭建")
    await p.user("lilei", ["user"], "李雷")
    await p.user("hanmei", ["user"], "韩梅")
    await p.user("boss", ["admin"], "管理员")
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
        assert opened.json()["created_by"] == "李雷" and opened.json()["app_title"] == "报销助手"
        cid = opened.json()["conversation_id"]
        assert (
            await api.post(f"/conversations/{cid}/messages", json={"message": "你好"})
        ).is_success
        draft = await api.post("/conversations", json={"app": "chat", "draft": True})
        assert draft.status_code == 403
    await p.service.wait_idle()
    async with signed(await p.signed_in("hanmei")) as api:
        for method, path, body in (
            ("GET", f"/conversations/{cid}", None),
            ("POST", f"/conversations/{cid}/messages", {"message": "我也问问"}),
            ("PATCH", f"/conversations/{cid}", {"title": "改名"}),
            ("DELETE", f"/conversations/{cid}", None),
        ):
            resp = await api.request(method, path, json=body)
            assert resp.status_code == 404 and resp.json()["detail"] == "没有找到这个会话"
        assert (await api.get("/conversations")).json() == []
    async with signed(await p.signed_in("boss")) as api:
        assert (await api.get("/conversations")).json() == []  # admins list their own
        seen = await api.get(f"/conversations/{cid}")
        assert seen.status_code == 200 and seen.json()["turns"][0]["reply"] == "回答"
        assert (await api.delete(f"/conversations/{cid}")).status_code == 204
    labels = [e["label"] for e in p.store.audit]
    assert {"新建会话", "在会话里发消息", "删除会话"} <= set(labels)


# Keeping and clearing


async def test_expired_conversations_are_deleted_and_their_turns_cleared():
    async with Platform(["答一", "答二"]) as p:
        await p.new_app("chat", llm_chat())
        old = await open_chat(p, "chat")
        turn = await say(p, old, "旧的问题")
        fresh = await open_chat(p, "chat")
        await say(p, fresh, "新的问题")
        long_ago = datetime.now(UTC) - timedelta(days=31)
        await p.service.conversations.update(old, updated_at=long_ago)

        assert await p.service.clear_expired(0) == 0  # 0: kept for good
        assert await p.service.clear_expired(40) == 0
        assert await p.service.clear_expired(30) == 1
        assert (await p.api.get(f"/conversations/{old}")).status_code == 404
        assert (await p.api.get(f"/conversations/{fresh}")).status_code == 200

        cleared = (await p.api.get(f"/runs/{turn['run_id']}")).json()
        assert cleared["status"] == "done" and cleared["cleared"] is True
        assert cleared["app_version"] == turn["app_version"]
        assert cleared["created_at"] == turn["created_at"]
        assert cleared["conversation"] is None and cleared["input"] == {}
        assert cleared["result"] is None and cleared["messages"] == []
        assert cleared["title"] is None
        listed = {r["run_id"]: r for r in (await p.api.get("/runs")).json()}
        assert listed[turn["run_id"]]["cleared"] is True


async def test_deleting_a_conversation_cancels_its_waiting_turn():
    async with Platform([]) as p:
        await p.new_app("orders", form_chat())
        cid = await open_chat(p, "orders")
        turn = await say(p, cid, "补录一单")
        assert turn["status"] == "waiting"
        assert (await p.api.delete(f"/conversations/{cid}")).status_code == 204
        await p.service.wait_idle()
        gone = (await p.api.get(f"/runs/{turn['run_id']}")).json()
        assert gone["status"] == "cancelled" and gone["cleared"] is True
        assert gone["error"] is None and gone["pending"] is None and gone["input"] == {}
        assert (await p.api.get(f"/conversations/{cid}")).status_code == 404
        assert (await p.api.delete(f"/conversations/{cid}")).status_code == 404


# The app API and MCP


async def chat_app(p: Access, name: str, config: dict) -> None:
    async with p.client() as api:
        for method, path, body in (
            ("POST", "/apps", {"app": name, "title": "报销助手", "from": {"blank": True}}),
            ("PUT", f"/apps/{name}/draft", {"config": config}),
            ("POST", f"/apps/{name}/draft/publish", {"note": "测试"}),
        ):
            resp = await api.request(method, path, json=body)
            assert resp.is_success, resp.text


async def test_the_app_api_and_mcp_go_on_with_a_conversation():
    p = Access()
    p.gateway.replies = ["一线城市 600 元", "二线城市 450 元", "单次的回答", "三线城市 350 元"]
    await chat_app(p, "chat", llm_chat())
    secret = await p.key("chat")
    other = await p.key("chat", "另一个系统")
    path = "/v1/apps/chat/runs"
    async with p.client() as api:
        first = await api.post(
            path,
            json={"input": {"message": "住宿标准？"}, "new_conversation": True, "wait": 5},
            headers=bearer(secret),
        )
        assert first.status_code == 201, first.text
        run = first.json()
        cid = run["conversation_id"]
        assert run["status"] == "done" and run["turn"] == 1
        assert run["result"] == {"reply": "一线城市 600 元"}
        second = await api.post(
            path,
            json={"input": {"message": "二线呢？"}, "conversation_id": cid, "wait": 5},
            headers=bearer(secret),
        )
        assert second.status_code == 201, second.text
        assert second.json()["turn"] == 2 and second.json()["conversation_id"] == cid
        assert sent(p, 1) == [
            ("user", "住宿标准？"),
            ("assistant", "一线城市 600 元"),
            ("user", "二线呢？"),
        ]
        got = await api.get(f"/v1/conversations/{cid}", headers=bearer(secret))
        assert got.status_code == 200
        assert got.json()["created_by"] == "API：CRM 系统"
        assert [t["reply"] for t in got.json()["turns"]] == ["一线城市 600 元", "二线城市 450 元"]

        # Only the key that opened it goes on with it.
        assert (await api.get(f"/v1/conversations/{cid}", headers=bearer(other))).status_code == 404
        refused = await api.post(
            path,
            json={"input": {"message": "我也问"}, "conversation_id": cid},
            headers=bearer(other),
        )
        assert refused.status_code == 404 and refused.json()["detail"] == "没有找到这个会话"
        both = await api.post(
            path,
            json={"input": {"message": "x"}, "conversation_id": cid, "new_conversation": True},
            headers=bearer(secret),
        )
        assert both.status_code == 422
        assert both.json()["detail"] == "new_conversation 和 conversation_id 只能给一个"
        empty = await api.post(
            path, json={"input": {}, "new_conversation": True}, headers=bearer(secret)
        )
        assert empty.status_code == 422 and "input.message" in empty.json()["detail"]
        # Nobody on the platform owns a key's conversation (no admins with sign-in off).
        assert (await api.get(f"/conversations/{cid}")).status_code == 404
        assert (await api.get("/conversations")).json() == []

        plain = await api.post(
            path, json={"input": {"message": "单次"}, "wait": 5}, headers=bearer(secret)
        )
        assert plain.json()["conversation_id"] is None and plain.json()["turn"] is None
        hello = await p.key("hello")
        not_chat = await api.post(
            "/v1/apps/hello/runs",
            json={"input": {"message": "你好"}, "new_conversation": True},
            headers=bearer(hello),
        )
        assert not_chat.status_code == 409 and not_chat.json()["detail"] == "这个应用没有开对话"

    async with mcp_client(p, "chat", secret) as client:
        tool = (await client.list_tools()).tools[0]
        assert set(tool.input_schema["properties"]) == {
            "message",
            "conversation_id",
            "new_conversation",
        }
        assert "可以多轮对话" in (tool.description or "")
        said: Any = await client.call_tool("chat", {"message": "三线呢？", "conversation_id": cid})
        assert not said.is_error, said.content[0].text
        assert said.structured_content["turn"] == 3
        assert said.structured_content["result"] == {"reply": "三线城市 350 元"}
        assert f"conversation_id：{cid}" in said.content[0].text
        wrong: Any = await client.call_tool("chat", {"message": "x", "conversation_id": "conv_x"})
        assert wrong.is_error and wrong.content[0].text == "没有找到这个会话"
    await chat_app(p, "ask-data", agent_chat())
    data_key = await p.key("ask-data")
    async with mcp_client(p, "ask-data", data_key) as client:
        tool = (await client.list_tools()).tools[0]
        # Going on takes only the message; the data source is given when a conversation opens.
        assert tool.input_schema["required"] == ["message"]
        assert tool.input_schema["properties"]["datasource"]["description"].endswith(
            "（开新会话或单次调用时必填）"
        )
        missing: Any = await client.call_tool(tool.name, {"message": "x", "new_conversation": True})
        assert missing.is_error and "数据源" in missing.content[0].text
    async with mcp_client(p, "hello", hello) as client:
        tool = (await client.list_tools()).tools[0]
        assert "conversation_id" not in tool.input_schema["properties"]
    await p.service.wait_idle()


async def test_only_the_last_turn_is_retried():
    async with Platform([]) as p:
        await p.new_app("chat", llm_chat())
        cid = await open_chat(p, "chat")
        p.gateway.status = 500
        failed = await say(p, cid, "一")
        assert failed["status"] == "error", failed
        p.gateway.status = 200
        p.gateway.replies = ["答二"]
        await say(p, cid, "二")
        refused = await p.api.post(f"/runs/{failed['run_id']}/retry")
        assert refused.status_code == 409
        assert refused.json()["detail"] == "会话里已经有后面的消息，这一轮不能重试"

        p.gateway.status = 500
        last = await say(p, cid, "三")
        assert last["status"] == "error"
        p.gateway.status = 200
        p.gateway.replies = ["答三"]
        retried = await p.api.post(f"/runs/{last['run_id']}/retry")
        assert retried.status_code == 202, retried.text
        await p.service.wait_idle()
        got = (await p.api.get(f"/conversations/{cid}")).json()
        assert [(t["status"], t["reply"]) for t in got["turns"]] == [
            ("error", None),
            ("done", "答二"),
            ("done", "答三"),
        ]


def test_a_cleared_run_cannot_be_retried():
    failed = record(1, "一", "error")
    assert retryable(failed)
    assert not retryable(dataclasses.replace(failed, input={}, cleared_at=datetime.now(UTC)))
