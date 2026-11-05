"""Agents calling apps (docs/api/agent-apps.md, "调用应用"): each enabled app an agent
names is a tool; a call starts a run of it like the app node does, and its answer goes
back to the model. Saving checks the apps an agent names."""

from typing import Any

import yaml

from aep.runtime import agent_app
from tests.test_agent_apps import new_agent, publish, save
from tests.test_conversations import open_chat, say
from tests.test_flow_app_node import LOOKUP
from tests.test_flow_tools import Platform, answer, call


def caller(*apps: dict[str, Any], **agent: Any) -> dict:
    config = agent_app.config("你是前台助手，按问题交给合适的同事。")
    config["agent"]["apps"] = list(apps)
    config["agent"].update(agent)
    return config


def to(app: str, when: str = "用户问到相关的事时", **more: Any) -> dict[str, Any]:
    return {"app": app, "when": when, **more}


async def helpers(p: Platform) -> None:
    """Two agents to call, and a flow app."""
    await new_agent(p, "policy")
    await publish(p, "policy", agent_app.config("你是报销政策助手。"))
    await p.api.patch("/apps/policy", json={"summary": "回答报销政策"})
    await new_agent(p, "orders")
    await publish(p, "orders", agent_app.config("你是订单助手。"))
    await p.new_app("lookup", LOOKUP)


async def problems(p: Platform, config: dict) -> dict[tuple, str]:
    resp = await save(p, "front", config)
    assert resp.status_code == 422, resp.text
    return {tuple(d["loc"][3:]): d["msg"] for d in resp.json()["detail"]}


async def test_saving_checks_the_apps_an_agent_calls():
    async with Platform([]) as p:
        await helpers(p)
        await new_agent(p, "front")
        await p.new_app(
            "by-source",
            {
                "graph": {
                    "nodes": [
                        {
                            "id": "start",
                            "type": "start",
                            "title": "开始",
                            "inputs": [
                                {
                                    "key": "ds",
                                    "label": "数据",
                                    "type": "datasource",
                                    "required": True,
                                }
                            ],
                        },
                        {
                            "id": "end",
                            "type": "end",
                            "title": "结束",
                            "outputs": [{"key": "ds", "from": ["{{start.ds}}"]}],
                        },
                    ],
                    "edges": [{"from": "start", "to": "end"}],
                }
            },
        )
        await p.api.put("/apps/orders/access", json={"apps": False})
        found = await problems(
            p,
            caller(
                to("etl"),
                to("front"),
                to("hello"),
                to("orders"),
                to("by-source"),
                to("nobody"),
                to("policy", when=""),
                to("policy"),
                to("lookup", when="查" * 501),
                to("lookup", colour="red"),
            ),
        )
        assert found == {
            (
                "apps",
                0,
                "app",
            ): "「自然语言 ETL 工作流」是代码应用，智能体只能调用编排应用和智能体应用",
            ("apps", 1, "app"): "不能调用自己",
            ("apps", 2, "app"): "应用「问答示例」里有人工节点，智能体等不了人",
            ("apps", 3, "app"): "应用「报销助手」不允许被其他应用调用",
            ("apps", 4, "app"): "应用「by-source」的必填输入「数据」要选数据源，模型填不了",
            ("apps", 5, "app"): "没有已发布的应用 nobody",
            ("apps", 6, "when"): "要写什么时候调用它",
            ("apps", 7, "app"): "应用 policy 选了两次",
            ("apps", 8, "when"): "什么时候调用最多 500 个字符",
            ("apps", 9, "colour"): "调用的应用没有 colour 这一项",
            ("apps", 9, "app"): "应用 lookup 选了两次",
        }
        too_many = await problems(p, caller(*[to(f"app-{i}") for i in range(11)]))
        assert too_many[("apps",)] == "最多调用 10 个应用"
        listed = (await p.api.get("/apps/front")).json()["options"]["apps"]
        by_app = {a["app"]: a for a in listed}
        assert "front" not in by_app
        assert by_app["policy"] == {
            "app": "policy",
            "title": "报销助手",
            "kind": "agent",
            "callable": True,
            "reason": None,
        }
        assert by_app["lookup"]["kind"] == "flow" and by_app["lookup"]["callable"]
        assert by_app["orders"]["reason"] == "应用「报销助手」不允许被其他应用调用"
        assert by_app["etl"]["kind"] == "code" and not by_app["etl"]["callable"]
        assert by_app["hello"]["reason"] == "应用「问答示例」里有人工节点，智能体等不了人"
        # Flow apps have no such list.
        assert "apps" not in (await p.api.get("/apps/lookup")).json()["options"]


async def test_an_agent_calls_the_apps_it_names_and_answers_from_them():
    async with Platform([]) as p:
        await helpers(p)
        await new_agent(p, "front")
        await publish(
            p,
            "front",
            caller(
                to("policy", "用户问报销政策时"),
                to("lookup", "用户问订单时"),
                to("orders", "从来不用", enabled=False),
            ),
        )
        p.gateway.replies = [
            call("a1", message="出差住宿标准是多少？"),
            "一线城市 600 元。",
            call("a2", question="42"),
            "订单 42 已发货",
            answer("住宿一线城市 600 元；订单 42 已发货。"),
        ]
        run = await p.run("front", {"message": "住宿标准，还有订单 42 到哪了？"})
        assert run["status"] == "done", run["error"]
        result = run["result"]
        assert result["reply"] == "住宿一线城市 600 元；订单 42 已发货。"
        assert result["grounded"] is True
        first, second = result["steps"]
        assert {k: first[k] for k in ("tool", "datasource", "app", "args", "error")} == {
            "tool": "call_app",
            "datasource": None,
            "app": "policy",
            "args": {"message": "出差住宿标准是多少？"},
            "error": None,
        }
        assert second["app"] == "lookup" and second["error"] is None

        system = p.gateway.requests[0]["body"]["messages"][0]["content"]
        assert "- a1：调用应用「报销助手」。回答报销政策 什么时候用：用户问报销政策时" in system
        assert "- a2：调用应用「lookup」。什么时候用：用户问订单时" in system
        assert "从来不用" not in system and "a3" not in system
        # The called agent's reply, the called flow's whole result.
        said = [m["content"] for m in p.gateway.requests[2]["body"]["messages"]]
        assert said[-1] == "工具 a1 返回：\n一线城市 600 元。"
        said = [m["content"] for m in p.gateway.requests[4]["body"]["messages"]]
        assert said[-1].startswith("工具 a2 返回：\n{") and "订单 42 已发货" in said[-1]
        # The called app gets only what the model wrote, no history.
        assert p.gateway.requests[1]["body"]["messages"][-1] == {
            "role": "user",
            "content": "出差住宿标准是多少？",
        }

        # Who calls whom, on the apps' 接入 tab: a stopped app is not called.
        callers = (await p.api.get("/apps/policy/access")).json()["apps"]["callers"]
        assert callers == [{"app": "front", "title": "报销助手", "version": 2, "via": "agent"}]
        assert (await p.api.get("/apps/orders/access")).json()["apps"]["callers"] == []

        called = (await p.api.get(f"/runs/{first['run_id']}")).json()
        assert called["app"] == "policy" and called["status"] == "done"
        assert called["trigger"]["kind"] == "app" and called["trigger"]["run_id"] == run["run_id"]
        assert (await p.service.get(first["run_id"])).record.entry == "apps"


async def test_an_agent_is_told_when_a_call_goes_wrong():
    async with Platform([]) as p:
        await helpers(p)
        await new_agent(p, "front")
        await publish(p, "front", caller(to("policy"), to("lookup")))
        p.gateway.replies = [
            call("a2", city="南京"),
            answer("查不到"),
        ]
        run = await p.run("front", {"message": "订单呢？"})
        assert run["status"] == "done", run["error"]
        [step] = run["result"]["steps"]
        assert step["app"] == "lookup" and step["run_id"] is None
        assert "调用「lookup」的输入不对" in step["error"]
        told = p.gateway.requests[1]["body"]["messages"][-1]["content"]
        assert told.startswith("工具 a2 返回：\n出错了：调用「lookup」的输入不对")
        # Nothing it called worked: the answer is held back.
        assert run["result"]["grounded"] is False

        # The app it calls calls it back: the chain refuses, and the model hears why.
        await publish(p, "policy", caller(to("front")))
        p.gateway.replies = [
            call("a1", message="问问你"),
            call("a1", message="再问回去"),
            answer("前台说不能再调"),
            answer("好的"),
        ]
        run = await p.run("front", {"message": "绕一圈"})
        assert run["status"] == "done", run["error"]
        [step] = run["result"]["steps"]
        inner = (await p.api.get(f"/runs/{step['run_id']}")).json()
        [back] = inner["result"]["steps"]
        assert back["app"] == "front" and "已经在调用链上" in back["error"]


async def test_an_app_closed_to_callers_is_left_out_at_run_time():
    async with Platform([]) as p:
        await helpers(p)
        await new_agent(p, "front")
        await publish(p, "front", caller(to("policy"), to("lookup")))
        await p.api.put("/apps/policy/access", json={"apps": False})
        p.gateway.replies = [answer("我先自己答")]
        run = await p.run("front", {"message": "住宿标准？"})
        system = p.gateway.requests[0]["body"]["messages"][0]["content"]
        assert "报销助手" not in system and "- a1：调用应用「lookup」" in system
        assert run["result"]["steps"] == []
        # With every app gone, the agent is a plain chat.
        await p.api.put("/apps/lookup/access", json={"apps": False})
        p.gateway.replies = ["你好"]
        run = await p.run("front", {"message": "你好"})
        assert run["result"]["reply"] == "你好" and run["result"]["grounded"] is True


async def test_an_agent_in_a_conversation_calls_apps_without_their_history():
    async with Platform([]) as p:
        await helpers(p)
        await new_agent(p, "front")
        await publish(p, "front", caller(to("policy")))
        p.gateway.replies = [
            call("a1", message="住宿标准？"),
            "一线 600 元。",
            answer("一线城市 600 元。"),
            call("a1", message="二线城市的住宿标准？"),
            "二线 450 元。",
            answer("二线城市 450 元。"),
        ]
        cid = await open_chat(p, "front")
        await say(p, cid, "住宿标准？")
        second = await say(p, cid, "那二线呢？")
        assert second["result"]["reply"] == "二线城市 450 元。"
        assert p.gateway.requests[4]["body"]["messages"] == [
            {"role": "system", "content": "你是报销政策助手。"},
            {"role": "user", "content": "二线城市的住宿标准？"},
        ]
        called = (await p.api.get(f"/runs/{second['result']['steps'][0]['run_id']}")).json()
        assert called["conversation"] is None


async def test_a_file_names_the_apps_it_calls_and_a_missing_one_is_a_problem():
    async with Platform([]) as p:
        await helpers(p)
        await new_agent(p, "front")
        await publish(p, "front", caller(to("policy")))
        text = (await p.api.get("/apps/front/versions/2/export")).text
        assert yaml.safe_load(text)["requires"]["apps"] == ["policy"]
        renamed = text.replace("app: policy", "app: gone")
        made = await p.api.post("/apps/import", json={"app": "front-copy", "yaml": renamed})
        assert made.status_code == 201, made.text
        assert made.json()["problems"] == [
            {"kind": "app", "name": "gone", "message": "没有这个已发布的应用，请在智能体里重新选择"}
        ]
        # The file waits in the draft until the app it calls is chosen again.
        copied = made.json()["app"]
        assert copied["current"]["config"]["agent"]["apps"] == []
        assert copied["draft"]["config"]["agent"]["apps"][0]["app"] == "gone"
