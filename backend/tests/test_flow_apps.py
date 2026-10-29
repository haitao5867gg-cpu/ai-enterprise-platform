"""Flow apps: checking a configuration, running every node type, and the API around them."""

import copy
import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.datasources import MemoryDataSourceStore
from aep.main import app
from aep.runtime.appconfig import ConfigError
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import builtin
from aep.runtime.flow.check import check_config
from aep.runtime.flow.nodes import TYPES, CheckEnv, holds
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import InvalidValues, RunService
from aep.runtime.store import MemoryRunStore
from aep.skills import MemorySkillStore
from tests.conftest import SETTINGS, FakeGateway

ENV = CheckEnv(models=["mock", "qwen"], skills=MemorySkillStore())


def routing() -> dict:
    return copy.deepcopy(builtin.TEMPLATES["intent-routing"].config)


def node(config: dict, node_id: str) -> dict:
    return next(n for n in config["graph"]["nodes"] if n["id"] == node_id)


async def problems(config: dict) -> dict[tuple, str]:
    with pytest.raises(ConfigError) as caught:
        await check_config(config, ENV)
    return {tuple(loc): message for loc, message in caught.value.problems}


def flow(*nodes: dict, edges: list[dict], env: dict | None = None) -> dict:
    start = {
        "id": "start",
        "type": "start",
        "title": "开始",
        "inputs": [{"key": "question", "label": "问题", "type": "text", "required": True}],
    }
    return {"env": env or {}, "graph": {"nodes": [start, *nodes], "edges": edges}}


def end(*sources: str) -> dict:
    return {
        "id": "end",
        "type": "end",
        "title": "结束",
        "outputs": [{"key": "out", "from": list(sources)}],
    }


# Checking


async def test_shipped_configs_pass_the_check():
    for config in [
        builtin.HELLO.config,
        builtin.blank(),
        *(t.config for t in builtin.TEMPLATES.values() if "knowledge_base" not in t.needs),
    ]:
        checked = await check_config(config, ENV)
        assert checked["schema_version"] == 1
        assert checked["env"] == {"vars": [], "secrets": []}


async def test_problems_are_located_by_node_and_setting():
    config = routing()
    config["graph"]["edges"] = [e for e in config["graph"]["edges"] if e.get("branch") != "product"]
    node(config, "draft")["prompt"] = "{{nope.text}} {{answer.text}} {{route.colour}}"
    node(config, "route")["model"] = "gpt-9"
    node(config, "review")["comment_required"] = ["maybe"]
    found = await problems(config)
    nodes = ("config", "graph", "nodes")
    assert found == {
        (*nodes, "route", "model"): "模型网关上没有 gpt-9",
        (*nodes, "review", "comment_required"): "必须写意见的选项要在选项里",
        (*nodes, "route", "branches", "product"): "分支「产品咨询」没有连到下一个节点",
    }

    # Once the structure is sound, references are checked.
    config = routing()
    node(config, "draft")["prompt"] = "{{nope.text}} {{answer.text}} {{route.colour}} {{env.x}}"
    messages = [m for loc, m in await _all(config) if loc == [*nodes, "draft", "prompt"]]
    assert messages == [
        "没有节点 nope",
        "运行到这里时节点「回答咨询」不一定运行过，不能引用它",
        "节点 route 没有产出 colour",
        "没有常量 x",
    ]


async def _all(config: dict) -> list:
    with pytest.raises(ConfigError) as caught:
        await check_config(config, ENV)
    return caught.value.problems


async def test_graph_rules():
    loop = flow(
        {"id": "a", "type": "template", "title": "A", "template": "x"},
        {"id": "b", "type": "template", "title": "B", "template": "y"},
        end("{{a.text}}"),
        edges=[
            {"from": "start", "to": "a"},
            {"from": "a", "to": "b"},
            {"from": "b", "to": "a"},
        ],
    )
    found = await problems(loop)
    assert found == {
        (
            "config",
            "graph",
            "nodes",
            "a",
        ): "这个循环没有经过人工确认、条件判断或意图分类节点，会一直循环下去",
        ("config", "graph", "nodes", "end"): "从开始走不到这个节点",
        ("config", "graph", "nodes", "start"): "从这个节点走不到结束节点",
    }

    # A loop whose way out is a template only (no decision) never ends.
    loop["graph"]["nodes"].append(
        {
            "id": "check",
            "type": "condition",
            "title": "判断",
            "rules": [
                {
                    "key": "done",
                    "label": "好了",
                    "conditions": [{"left": "{{a.text}}", "op": "not_empty"}],
                }
            ],
        }
    )
    loop["graph"]["edges"] = [
        {"from": "start", "to": "a"},
        {"from": "a", "to": "b"},
        {"from": "b", "to": "a"},
        {"from": "check", "branch": "done", "to": "end"},
        {"from": "check", "branch": "else", "to": "a"},
    ]
    found = await problems(loop)
    assert found[("config", "graph", "nodes", "check")] == "从开始走不到这个节点"

    bad = flow(
        {"id": "start", "type": "template", "title": "重复", "template": "x"},
        {"id": "env", "type": "template", "title": "保留", "template": "x"},
        {
            "id": "c",
            "type": "condition",
            "title": "判断",
            "rules": [],
            "on_error": {"then": "stop"},
        },
        end("{{secret.k}}"),
        edges=[
            {"from": "start", "to": "end"},
            {"from": "end", "to": "start"},
            {"from": "x", "to": "end"},
        ],
    )
    found = await problems(bad)
    nodes = ("config", "graph", "nodes")
    assert found[(*nodes, "start", "id")] == "标识 start 重复"
    assert found[(*nodes, "env", "id")] == "env 是保留的标识"
    assert found[(*nodes, "c", "on_error")] == "条件判断节点不能设出错处理"
    assert found[(*nodes, "c", "rules")] == "规则要有 1–10 项"
    assert found[("config", "graph", "edges", 1, "to")] == "不能连回开始节点"
    assert found[("config", "graph", "edges", 2, "from")] == "没有节点 x"


async def test_secrets_only_in_tool_args_and_on_error_defaults():
    config = flow(
        {
            "id": "ask",
            "type": "llm",
            "title": "问",
            "prompt": "{{secret.token}}",
            "on_error": {"then": "default", "default": {"txt": "?"}},
        },
        end("{{ask.text}}"),
        edges=[{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
        env={"secrets": ["token"]},
    )
    found = await problems(config)
    assert found == {
        ("config", "graph", "nodes", "ask", "on_error", "default", "txt"): "节点没有产出 txt"
    }
    node(config, "ask")["on_error"]["default"] = {"text": "?"}
    found = await problems(config)
    assert found == {("config", "graph", "nodes", "ask", "prompt"): "密钥只能写在工具节点的参数里"}


def test_conditions():
    assert holds("gt", "600", 500)
    assert not holds("gt", "abc", 500)
    assert holds("eq", "1.0", 1)
    assert holds("contains", ["a", "b"], "a")
    assert holds("contains", "退货申请", "退货")
    assert holds("empty", "", None) and holds("not_empty", 0, None)


# Running


class Platform:
    def __init__(self, replies: list[str], status: int = 200) -> None:
        self.gateway = FakeGateway(replies, status)
        self.sources = MemoryDataSourceStore()
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            default_apps(),
            AppContext(self.gateway.client, SETTINGS, datasources=self.sources),
            skills=MemorySkillStore(),
        )
        self.catalog = AppCatalog(self.service, self.sources, SETTINGS, models=self.models)

    async def models(self) -> list[str]:
        return ["mock", "qwen"]

    async def __aenter__(self) -> "Platform":
        app.state.run_service = self.service
        app.state.catalog = self.catalog
        app.state.skills = self.service.skills
        app.state.datasources = self.sources
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.api = await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def new_app(self, name: str, config: dict) -> None:
        resp = await self.api.post(
            "/apps", json={"app": name, "title": name, "from": {"blank": True}}
        )
        assert resp.status_code == 201, resp.text
        resp = await self.api.put(f"/apps/{name}/draft", json={"config": config})
        assert resp.status_code == 200, resp.text
        resp = await self.api.post(f"/apps/{name}/draft/publish", json={"note": "测试"})
        assert resp.status_code == 200, resp.text

    async def run(self, name: str, input: dict) -> dict:
        resp = await self.api.post("/runs", json={"app": name, "input": input})
        assert resp.status_code == 201, resp.text
        await self.service.wait_idle()
        return (await self.api.get(f"/runs/{resp.json()['run_id']}")).json()

    async def resume(self, run: dict, **body) -> dict:
        resp = await self.api.post(f"/runs/{run['run_id']}/resume", json=body)
        assert resp.status_code == 202, resp.text
        await self.service.wait_idle()
        return (await self.api.get(f"/runs/{run['run_id']}")).json()


def cls(name: str) -> str:
    return json.dumps({"class": name})


async def test_intent_routing_runs_each_branch():
    replies = [
        cls("after_sales"),
        "很抱歉……第一稿",
        "很抱歉……第二稿",
        cls("product"),
        "价格是 99 元",
        cls("天气"),
    ]
    async with Platform(replies) as p:
        await p.new_app("support", routing())

        run = await p.run("support", {"question": "我要退货"})
        assert run["status"] == "waiting"
        assert run["pending"]["option_labels"] == {"approve": "确认", "reject": "驳回"}
        assert run["pending"]["details"] == {
            "type": "flow_human",
            "node": "review",
            "show": "很抱歉……第一稿",
        }
        refused = await p.api.post(f"/runs/{run['run_id']}/resume", json={"decision": "reject"})
        assert refused.status_code == 422
        run = await p.resume(run, decision="reject", comment="语气再软一点")
        assert run["pending"]["details"]["show"] == "很抱歉……第二稿"
        redraft = p.gateway.requests[2]["body"]["messages"][-1]["content"]
        assert "语气再软一点" in redraft
        run = await p.resume(run, decision="approve", actor="小王")
        assert run["status"] == "done"
        assert run["result"] == {"reply": "很抱歉……第二稿"}
        assert run["messages"] == [
            {"role": "user", "content": "我要退货"},
            {"role": "assistant", "content": "很抱歉……第一稿"},
            {"role": "user", "content": "驳回：语气再软一点"},
            {"role": "assistant", "content": "很抱歉……第二稿"},
            {"role": "user", "content": "确认"},
        ]

        assert (await p.run("support", {"question": "多少钱"}))["result"] == {
            "reply": "价格是 99 元"
        }
        other = await p.run("support", {"question": "明天天气"})
        assert other["result"] == {"reply": "这个问题已转给人工客服。"}

        classifier = p.gateway.requests[0]["body"]["messages"]
        assert "after_sales：售后" in classifier[0]["content"]
        assert classifier[1]["content"] == "我要退货"

        flow_view = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()
        assert flow_view["design"]["title"] == "support"
        assert flow_view["design"]["pattern"].startswith("编排应用")
        nodes = {n["id"]: n for n in flow_view["nodes"]}
        assert nodes["route"]["type"] == "classifier" and nodes["route"]["kind"] == "model"
        assert nodes["route"]["model"] == "mock"
        assert nodes["__start__"]["type"] is None
        labels = {(e["source"], e["target"]): e["label"] for e in flow_view["edges"]}
        assert labels[("route", "draft")] == "售后"
        assert labels[("route", "handoff")] == "都不是"
        assert labels[("draft", "review")] is None
        assert [s["node"] for s in flow_view["steps"]][:3] == ["start", "route", "draft"]


async def test_json_output_condition_and_constants():
    config = flow(
        {
            "id": "extract",
            "type": "llm",
            "title": "抽取金额",
            "prompt": "{{start.question}}",
            "output": {
                "kind": "json",
                "fields": [{"key": "amount", "type": "number", "description": "金额"}],
            },
        },
        {
            "id": "big",
            "type": "condition",
            "title": "大额？",
            "rules": [
                {
                    "key": "big",
                    "label": "大额",
                    "conditions": [
                        {"left": "{{extract.amount}}", "op": "gt", "right": "{{env.limit}}"}
                    ],
                }
            ],
        },
        {
            "id": "yes",
            "type": "template",
            "title": "要审批",
            "template": "{{extract.amount}} 超过 {{env.limit}}，要审批",
        },
        {"id": "no", "type": "template", "title": "直接退", "template": "直接退款"},
        end("{{yes.text}}", "{{no.text}}"),
        edges=[
            {"from": "start", "to": "extract"},
            {"from": "extract", "to": "big"},
            {"from": "big", "branch": "big", "to": "yes"},
            {"from": "big", "branch": "else", "to": "no"},
            {"from": "yes", "to": "end"},
            {"from": "no", "to": "end"},
        ],
        env={"vars": [{"key": "limit", "type": "number", "value": 500}]},
    )
    # The first reply misses the field and is asked again.
    replies = ['{"amt": 1}', '```json\n{"amount": 800}\n```', '{"amount": 20}']
    async with Platform(replies) as p:
        await p.new_app("refund", config)
        run = await p.run("refund", {"question": "退 800 元"})
        assert run["result"] == {"out": "800 超过 500，要审批"}
        assert "amount（number）" in p.gateway.requests[0]["body"]["messages"][0]["content"]
        assert "缺少字段 amount" in p.gateway.requests[1]["body"]["messages"][-1]["content"]
        assert (await p.run("refund", {"question": "退 20 元"}))["result"] == {"out": "直接退款"}


def without_branch(config: dict) -> dict:
    """The failing flow without its error branch."""
    config["graph"]["edges"] = [
        e for e in config["graph"]["edges"] if "sorry" not in (e["from"], e["to"])
    ]
    config["graph"]["nodes"] = [n for n in config["graph"]["nodes"] if n["id"] != "sorry"]
    node(config, "end")["outputs"][0]["from"] = ["{{ask.text}}"]
    return config


def failing_flow(on_error: dict) -> dict:
    return flow(
        {
            "id": "ask",
            "type": "llm",
            "title": "问模型",
            "prompt": "{{start.question}}",
            "on_error": on_error,
        },
        {
            "id": "sorry",
            "type": "template",
            "title": "道歉",
            "template": "出错了：{{ask.error_type}}",
        },
        end("{{ask.text}}", "{{sorry.text}}"),
        edges=[
            {"from": "start", "to": "ask"},
            {"from": "ask", "to": "end"},
            {"from": "ask", "branch": "error", "to": "sorry"},
            {"from": "sorry", "to": "end"},
        ],
    )


async def test_on_error_retries_then_branches_or_uses_defaults():
    async with Platform(["x"], status=500) as p:
        await p.new_app(
            "fails", failing_flow({"retry": {"max": 2, "interval_seconds": 0}, "then": "branch"})
        )
        run = await p.run("fails", {"question": "你好"})
        assert run["status"] == "done"
        assert run["result"] == {"out": "出错了：model_error"}
        # Three tries; the client retries each once more on a 500.
        assert len(p.gateway.requests) == 6

        config = without_branch(
            failing_flow({"then": "default", "default": {"text": "暂时无法回答"}})
        )
        await p.new_app("defaults", config)
        assert (await p.run("defaults", {"question": "你好"}))["result"] == {"out": "暂时无法回答"}

        config = without_branch(failing_flow({"then": "stop"}))
        await p.new_app("stops", config)
        stopped = await p.run("stops", {"question": "你好"})
        assert stopped["status"] == "error"
        assert stopped["error"].startswith("节点「问模型」(ask)：调用模型失败")


async def test_form_mode_asks_for_values():
    config = flow(
        {
            "id": "ask_order",
            "type": "human",
            "title": "补充订单号",
            "mode": "form",
            "prompt": "请提供订单号",
            "fields": [
                {"key": "order_id", "label": "订单号", "type": "text", "required": True},
                {"key": "count", "label": "件数", "type": "number"},
            ],
        },
        {
            "id": "say",
            "type": "template",
            "title": "回执",
            "template": "{{ask_order.actor}} 提交了 {{ask_order.order_id}}",
        },
        end("{{say.text}}"),
        edges=[
            {"from": "start", "to": "ask_order"},
            {"from": "ask_order", "to": "say"},
            {"from": "say", "to": "end"},
        ],
    )
    async with Platform(["x"]) as p:
        await p.new_app("orders", config)
        run = await p.run("orders", {"question": "查订单"})
        pending = run["pending"]
        assert (pending["kind"], pending["options"], pending["option_labels"]) == (
            "form",
            ["submit"],
            {"submit": "提交"},
        )
        assert [f["key"] for f in pending["fields"]] == ["order_id", "count"]
        bad = await p.api.post(
            f"/runs/{run['run_id']}/resume",
            json={"decision": "submit", "values": {"order_id": " ", "count": "两件"}},
        )
        assert bad.status_code == 422
        assert {tuple(d["loc"]): d["msg"] for d in bad.json()["detail"]} == {
            ("body", "values", "order_id"): "订单号不能为空",
            ("body", "values", "count"): "件数必须是数字",
        }
        run = await p.resume(run, decision="submit", values={"order_id": "A1001"}, actor="小王")
        assert run["result"] == {"out": "小王 提交了 A1001"}
        assert run["messages"][-1] == {"role": "user", "content": "订单号：A1001；件数："}


async def test_a_node_stops_the_run_past_its_visit_limit():
    config = copy.deepcopy(builtin.HELLO.config)
    node(config, "draft")["max_visits"] = 1
    async with Platform(["第一版", "第二版"]) as p:
        await p.new_app("once", config)
        run = await p.run("once", {"text": "你好"})
        run = await p.resume(run, decision="reject", comment="再来")
        assert run["status"] == "error"
        assert run["error"] == "节点「回答」(draft)：已经运行了 1 次，达到上限 1 次"


async def test_input_must_fit_the_start_node():
    async with Platform(["x"]) as p:
        resp = await p.api.post("/runs", json={"app": "hello", "input": {"text": ""}})
        assert resp.status_code == 422
        assert resp.json()["detail"] == [
            {"loc": ["body", "input", "text"], "msg": "问题不能为空", "type": "value_error"}
        ]


# The API


async def test_node_types_and_templates():
    async with Platform(["x"]) as p:
        types = (await p.api.get("/node-types")).json()
        assert [t["type"] for t in types] == list(TYPES)
        llm_type = next(t for t in types if t["type"] == "llm")
        assert llm_type["error_handling"] is True
        assert llm_type["presets"][0]["key"] == "extract"
        assert next(t for t in types if t["type"] == "start")["max_count"] == 1
        templates = (await p.api.get("/app-templates")).json()
        assert [t["id"] for t in templates] == [
            "qa-approval",
            "intent-routing",
            "datasource-qa",
            "knowledge-qa",
            "knowledge-agent",
        ]
        assert templates[2]["needs"] == ["datasource"]


async def test_create_rename_copy_and_archive_flow_apps():
    async with Platform(["回答"]) as p:
        made = await p.api.post(
            "/apps",
            json={
                "app": "support",
                "title": "客服路由",
                "summary": "分流",
                "from": {"template": "intent-routing"},
                "actor": "张三",
            },
        )
        assert made.status_code == 201, made.text
        detail = made.json()
        assert (detail["kind"], detail["current_version"], detail["title"]) == (
            "flow",
            1,
            "客服路由",
        )
        assert detail["current"]["note"] == "从模板「意图分类路由」新建"
        assert detail["current"]["created_by"] == "张三"
        assert detail["inputs"][0]["key"] == "question"
        assert detail["secrets"] == []
        labels = {(e["source"], e["target"]): e["label"] for e in detail["edges"]}
        assert labels[("route", "answer")] == "产品咨询"
        assert {n["id"]: n["type"] for n in detail["nodes"]}["review"] == "human"

        again = await p.api.post("/apps", json={"app": "support", "title": "重名"})
        assert again.status_code == 409
        assert (await p.api.post("/apps", json={"app": "etl", "title": "重名"})).status_code == 409
        bad = await p.api.post("/apps", json={"app": "x2", "title": "复制", "from": {"app": "etl"}})
        assert bad.status_code == 422
        assert (
            await p.api.post("/apps", json={"app": "Bad_Name", "title": "x"})
        ).status_code == 422

        copied = await p.api.post(
            "/apps", json={"app": "support-2", "title": "副本", "from": {"app": "support"}}
        )
        assert copied.json()["current"]["note"] == "复制自 support 版本 1"
        blank = (await p.api.post("/apps", json={"app": "blank", "title": "空白"})).json()
        assert [n["type"] for n in blank["nodes"]] == [None, "start", "llm", "end", None]
        run = await p.run("blank", {"question": "你好"})
        assert run["result"] == {"answer": "回答"}

        renamed = await p.api.patch("/apps/support", json={"title": "新名字"})
        assert renamed.json()["title"] == "新名字"
        assert (await p.api.patch("/apps/etl", json={"title": "x"})).status_code == 409
        assert (await p.api.delete("/apps/etl")).status_code == 409

        listed = {a["app"]: a for a in (await p.api.get("/apps")).json()}
        assert listed["etl"]["kind"] == "code" and listed["etl"]["created_at"] is None
        assert listed["support"]["kind"] == "flow"
        assert (await p.api.delete("/apps/blank")).status_code == 204
        assert "blank" not in {a["app"] for a in (await p.api.get("/apps")).json()}
        assert (await p.api.get("/apps/blank")).status_code == 404
        assert (
            await p.api.post("/runs", json={"app": "blank", "input": {"question": "x"}})
        ).status_code == 404
        # Its runs can still be read.
        assert (await p.api.get(f"/runs/{run['run_id']}")).json()["status"] == "done"


async def test_draft_runs():
    async with Platform(["回答"]) as p:
        no_draft = await p.api.post(
            "/runs", json={"app": "hello", "input": {"text": "x"}, "draft": True}
        )
        assert no_draft.status_code == 409
        both = await p.api.post(
            "/runs", json={"app": "hello", "input": {}, "draft": True, "app_version": 1}
        )
        assert both.status_code == 422

        config = copy.deepcopy(builtin.HELLO.config)
        node(config, "draft")["model"] = "qwen"
        await p.api.put("/apps/hello/draft", json={"config": config})
        resp = await p.api.post(
            "/runs", json={"app": "hello", "input": {"text": "x"}, "draft": True}
        )
        assert resp.status_code == 201
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert (run["app_version"], run["draft"]) == (None, True)
        assert p.gateway.requests[-1]["body"]["model"] == "qwen"
        assert (await p.api.get("/runs")).json()[0]["draft"] is True


async def test_hello_carries_over_a_configuration_from_before_it_was_a_flow_app():
    async with Platform(["回答"]) as p:
        await p.service.ensure_versions("hello")
        legacy = {"nodes": {"draft": {"model": "qwen", "skills": [], "params": {}}}}
        await p.service.versions.save_draft("hello", legacy, None)
        await p.service.versions.publish("hello", "旧格式", None)
        detail = (await p.api.get("/apps/hello")).json()
        assert node(detail["current"]["config"], "draft")["model"] == "qwen"
        run = await p.run("hello", {"text": "你好"})
        assert run["status"] == "waiting"
        assert p.gateway.requests[0]["body"]["model"] == "qwen"


async def test_values_are_checked_by_the_service():
    async with Platform(["x"]) as p:
        with pytest.raises(InvalidValues):
            await p.service.create("hello", {})
