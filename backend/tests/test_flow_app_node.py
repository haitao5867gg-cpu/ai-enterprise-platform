"""The app node: one flow app calls another as a step (docs/api/flow-apps.md, "app 调用应用").
Saving checks it against the called app; a run starts a run of it marked with the
caller, waits and takes its result; a called app that waits for a person, fails or is
already on the chain fails the node."""

from typing import Any

import pytest

from tests.test_flow_apps import end, flow
from tests.test_flow_tools import Platform
from tests.test_projects import project


def ask(prompt: str = "{{start.question}}") -> dict:
    return {"id": "ask", "type": "llm", "title": "回答", "prompt": prompt}


LOOKUP = flow(
    ask("查一下：{{start.question}}"),
    {
        "id": "end",
        "type": "end",
        "title": "结束",
        "outputs": [
            {"key": "reply", "from": ["{{ask.text}}"]},
            {"key": "source", "from": ["{{start.question}}"]},
        ],
    },
    edges=[{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
)


def call(app: str = "lookup", **changes: Any) -> dict:
    return {
        "id": "call",
        "type": "app",
        "title": "查订单",
        "app": app,
        "input": {"question": "订单 {{start.question}}"},
        "outputs": ["reply"],
        **changes,
    }


def caller(node: dict | None = None) -> dict:
    return flow(
        node or call(),
        end("{{call.reply}}"),
        edges=[{"from": "start", "to": "call"}, {"from": "call", "to": "end"}],
    )


async def publish(p: Platform, name: str, config: dict) -> None:
    resp = await save(p, name, config)
    assert resp.status_code == 200, resp.text
    resp = await p.api.post(f"/apps/{name}/draft/publish", json={"note": "改"})
    assert resp.status_code == 200, resp.text


async def save(p: Platform, name: str, config: dict) -> Any:
    if (await p.api.get(f"/apps/{name}")).status_code == 404:
        made = await p.api.post("/apps", json={"app": name, "title": name})
        assert made.status_code == 201, made.text
    return await p.api.put(f"/apps/{name}/draft", json={"config": config})


@pytest.mark.parametrize(
    ("changes", "loc", "said"),
    [
        ({"app": ""}, "app", "要选择被调用的应用"),
        ({"app": "nope"}, "app", "没有应用 nope"),
        ({"app": "etl"}, "app", "只能调用编排应用，「自然语言 ETL 工作流」是代码应用"),
        ({"input": {"question": "x", "city": "南京"}}, "input.city", "应用「lookup」没有输入 city"),
        ({"input": {}}, "input.question", "要填「问题」"),
        ({"outputs": ["answer"]}, "outputs.0", "应用「lookup」的结果里没有 answer"),
        ({"outputs": ["run_id"]}, "outputs.0", "run_id 是这个节点自带的产出，不用再选"),
        ({"outputs": ["reply", "reply"]}, "outputs.1", "结果字段 reply 重复"),
        ({"timeout_seconds": 5}, "timeout_seconds", "最长等待在 10–3600 秒之间"),
    ],
)
async def test_saving_checks_the_called_app(changes, loc, said):
    async with Platform([]) as p:
        await p.new_app("lookup", LOOKUP)
        resp = await save(p, "front", caller(call(**changes)))
        assert resp.status_code == 422, resp.text
        found = {".".join(str(x) for x in d["loc"][5:]): d["msg"] for d in resp.json()["detail"]}
        assert found.get(loc) == said, found


async def test_a_run_calls_the_app_and_takes_its_result():
    async with Platform(["订单 42 已发货"]) as p:
        await p.new_app("lookup", LOOKUP)
        await p.new_app("front", caller(call(outputs=["reply", "source"])))
        types = (await p.api.get("/node-types")).json()
        app_type = next(t for t in types if t["type"] == "app")
        assert app_type["label"] == "调用应用" and app_type["error_handling"]
        assert [o["key"] for o in app_type["outputs"]] == ["result", "run_id"]

        resp = await p.api.post(
            "/runs", json={"app": "front", "input": {"question": "42"}, "requester": "张三"}
        )
        assert resp.status_code == 201, resp.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"out": "订单 42 已发货"}
        assert p.gateway.requests[0]["body"]["messages"][-1]["content"] == "查一下：订单 42"

        called = next(r for r in (await p.api.get("/runs")).json() if r["app"] == "lookup")
        assert called["trigger"] == {
            "kind": "app",
            "run_id": run["run_id"],
            "node": "call",
            "title": "front",
        }
        assert called["created_by"] == "张三" and called["status"] == "done"
        said = [m["content"] for m in run["messages"] if m.get("node") == "call"]
        assert said == [f"调用「lookup」：运行 {called['run_id'][:8]} 已完成。"]
        flow_view = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()
        step = next(s for s in flow_view["steps"] if s["node"] == "call")
        outputs = step["output"]["vars"]["call"]
        assert outputs["reply"] == "订单 42 已发货" and outputs["source"] == "订单 42"
        assert outputs["result"] == {"reply": "订单 42 已发货", "source": "订单 42"}
        assert outputs["run_id"] == called["run_id"]


async def test_a_called_app_that_waits_for_a_person_is_stopped():
    human = {
        "id": "check",
        "type": "human",
        "title": "确认",
        "mode": "approval",
        "prompt": "对吗？",
        "options": [{"key": "approve", "label": "对"}, {"key": "reject", "label": "不对"}],
        "comment_required": [],
    }
    waits = flow(
        human,
        end("{{check.decision}}"),
        edges=[
            {"from": "start", "to": "check"},
            {"from": "check", "branch": "approve", "to": "end"},
            {"from": "check", "branch": "reject", "to": "end"},
        ],
    )
    waits["graph"]["nodes"][-1]["outputs"] = [{"key": "reply", "from": ["{{check.decision}}"]}]
    async with Platform([]) as p:
        await p.new_app("lookup", waits)
        await p.new_app("front", caller())
        run = await p.run("front", {"question": "42"})
        assert run["status"] == "error"
        assert run["failed"]["error_type"] == "input_error"
        assert run["error"].endswith(
            "应用「lookup」要人工处理，不能被调用；人工确认请放在调用它的应用里"
        )
        called = next(r for r in (await p.api.get("/runs")).json() if r["app"] == "lookup")
        assert called["status"] == "cancelled"


async def test_a_failed_called_run_fails_the_node_and_on_error_takes_over():
    broken = flow(
        {
            "id": "calc",
            "type": "code",
            "title": "算",
            "code": "def main(inputs):\n    return {'n': 1 / 0}",
            "inputs": {},
            "outputs": [{"key": "n", "type": "number"}],
        },
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "reply", "from": ["{{calc.n}}"]}],
        },
        edges=[{"from": "start", "to": "calc"}, {"from": "calc", "to": "end"}],
    )
    async with Platform([]) as p:
        await p.new_app("lookup", broken)
        await p.new_app("front", caller())
        run = await p.run("front", {"question": "42"})
        assert run["status"] == "error"
        assert run["failed"] == {
            "node": "call",
            "title": "查订单",
            "error_type": "app_error",
            "retryable": True,
        }
        assert "调用「lookup」没有完成" in run["error"]

        softened = caller(call(on_error={"then": "default", "default": {"reply": "查不到"}}))
        await publish(p, "front", softened)
        again = await p.run("front", {"question": "42"})
        assert again["status"] == "done", again["error"]
        assert again["result"] == {"out": "查不到"}


async def test_an_app_already_on_the_chain_is_not_called_again():
    async with Platform([]) as p:
        first = caller(call(app="hello", input={"text": "x"}, outputs=[]))
        first["graph"]["nodes"][-1]["outputs"] = [{"key": "out", "from": ["{{call.run_id}}"]}]
        await p.new_app("front", first)
        loop = caller(call(app="front", input={"question": "{{start.question}}"}, outputs=[]))
        loop["graph"]["nodes"][-1]["outputs"] = [{"key": "out", "from": ["{{call.run_id}}"]}]
        await publish(p, "front", loop)
        run = await p.run("front", {"question": "42"})
        assert run["status"] == "error"
        assert run["error"].endswith("应用「front」已经在调用链上，不能再调用它")
        assert [r["app"] for r in (await p.api.get("/runs")).json()] == ["front"]


async def test_the_called_app_binds_the_callers_project():
    bound = flow(
        ask("{{param.update_month}} {{start.question}}"),
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "reply", "from": ["{{ask.text}}"]}],
        },
        edges=[{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
    )
    bound["graph"]["nodes"][0]["project"] = "optional"
    front = caller()
    front["graph"]["nodes"][0]["project"] = "required"
    async with Platform(["好"]) as p:
        await p.api.post("/projects", json=project())
        await p.new_app("lookup", bound)
        await p.new_app("front", front)
        made = await p.api.post(
            "/runs",
            json={
                "app": "front",
                "input": {"question": "42"},
                "project": {
                    "project_id": "jade-ibf",
                    "environment": "prod",
                    "params": {"version": 3},
                },
            },
        )
        assert made.status_code == 201, made.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{made.json()['run_id']}")).json()
        assert run["status"] == "done", run["error"]
        called = next(r for r in (await p.api.get("/runs")).json() if r["app"] == "lookup")
        assert called["project"]["environment"] == "prod"
        assert called["project"]["params"] == {"update_month": "2026-10", "version": 3}
        assert p.gateway.requests[0]["body"]["messages"][-1]["content"] == "2026-10 订单 42"


async def test_a_restarted_caller_waits_for_the_run_it_started():
    async with Platform(["订单 42 已发货"]) as p:
        await p.new_app("lookup", LOOKUP)
        await p.new_app("front", caller())
        first = await p.run("front", {"question": "42"})
        called = next(r for r in (await p.api.get("/runs")).json() if r["app"] == "lookup")
        store = p.service._store
        # As if the caller had stopped while its call was still running.
        await store.set_status(called["run_id"], "running")
        found = await store.called_by(first["run_id"], "call")
        assert found is not None and found.run_id == called["run_id"]
        assert await store.called_by(first["run_id"], "other") is None
