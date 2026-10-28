"""Flow apps with tools: tool and agent nodes, secrets, trying one node, export and import."""

import copy
import json

import httpx
import yaml
from langgraph.checkpoint.memory import InMemorySaver
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aep.datasources import MemoryDataSourceStore
from aep.main import app
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import builtin
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.skills import MemorySkillStore, SkillContent
from aep.tools.mcp import McpTools
from tests import fake_connector
from tests.conftest import SETTINGS, FakeGateway

CRM_URL = "http://crm.test/mcp"


def crm() -> MCPServer:
    """A connector whose tool needs a token, like a SaaS API."""
    server = MCPServer("fake-crm")

    @server.tool()
    def get_order(order_id: str, token: str) -> dict:
        """Look an order up."""
        if token != "s3cret":
            raise ToolError("bad token")
        if order_id == "boom":
            raise ToolError("order service is down")
        return {"order_id": order_id, "status": "shipped"}

    return server


class Platform:
    def __init__(self, replies: list[str]) -> None:
        self.gateway = FakeGateway(replies)
        self.sources = MemoryDataSourceStore()
        shop, orders = fake_connector.build(), crm()
        servers = {fake_connector.URL: shop, CRM_URL: orders}
        self.skills = MemorySkillStore()
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            default_apps(),
            AppContext(
                self.gateway.client,
                SETTINGS,
                datasources=self.sources,
                tools=McpTools(5, resolve=lambda url: servers[url]),
            ),
            skills=self.skills,
        )
        self.catalog = AppCatalog(self.service, self.sources, SETTINGS, models=self.models)

    async def models(self) -> list[str]:
        return ["mock", "qwen"]

    async def __aenter__(self) -> "Platform":
        self.shop = (
            await self.sources.create("示例库", "postgres", fake_connector.URL)
        ).datasource_id
        self.crm = (await self.sources.create("CRM", "postgres", CRM_URL)).datasource_id
        app.state.run_service = self.service
        app.state.catalog = self.catalog
        app.state.skills = self.skills
        app.state.datasources = self.sources
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.api = await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def new_app(self, name: str, config: dict) -> None:
        resp = await self.api.post("/apps", json={"app": name, "title": name})
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


def order_flow(datasource: str, on_error: dict | None = None) -> dict:
    lookup = {
        "id": "lookup",
        "type": "tool",
        "title": "查订单",
        "datasource": datasource,
        "tool": "get_order",
        "args": {"order_id": "{{start.order_id}}", "token": "{{secret.crm_token}}"},
    }
    edges = [{"from": "start", "to": "lookup"}, {"from": "lookup", "to": "end"}]
    if on_error:
        lookup["on_error"] = on_error
        edges.append({"from": "lookup", "branch": "error", "to": "sorry"})
        edges.append({"from": "sorry", "to": "end"})
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "inputs": [{"key": "order_id", "label": "订单号", "type": "text", "required": True}],
        },
        lookup,
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "out", "from": ["{{lookup.result}}", "{{sorry.text}}"]}],
        },
    ]
    if on_error:
        nodes.append(
            {
                "id": "sorry",
                "type": "template",
                "title": "道歉",
                "template": "{{lookup.error_message}}",
            }
        )
    else:
        nodes[2]["outputs"][0]["from"] = ["{{lookup.result}}"]
    return {"env": {"secrets": ["crm_token"]}, "graph": {"nodes": nodes, "edges": edges}}


async def test_tool_node_reads_secrets_and_handles_errors():
    async with Platform(["x"]) as p:
        await p.new_app(
            "orders",
            order_flow(p.crm, {"retry": {"max": 1, "interval_seconds": 0}, "then": "branch"}),
        )
        detail = (await p.api.get("/apps/orders")).json()
        assert detail["secrets"] == [{"name": "crm_token", "set": False, "updated_at": None}]
        unset = await p.run("orders", {"order_id": "A1"})
        assert unset["result"] == {"out": "密钥 crm_token 没有值，请在应用设置里填写"}

        assert (
            await p.api.put("/apps/orders/secrets/crm_token", json={"value": "s3cret"})
        ).status_code == 204
        detail = (await p.api.get("/apps/orders")).json()
        assert detail["secrets"][0]["set"] is True
        assert "s3cret" not in json.dumps(detail)

        run = await p.run("orders", {"order_id": "A1"})
        assert run["result"] == {"out": {"order_id": "A1", "status": "shipped"}}
        flow = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()
        assert "s3cret" not in json.dumps(flow)
        assert {n["id"]: n["kind"] for n in flow["nodes"]}["lookup"] == "tool"

        failed = await p.run("orders", {"order_id": "boom"})
        assert "order service is down" in failed["result"]["out"]

        assert (await p.api.delete("/apps/orders/secrets/crm_token")).status_code == 204
        assert (await p.api.delete("/apps/orders/secrets/crm_token")).status_code == 204
        assert (await p.api.put("/apps/orders/secrets/Bad", json={"value": "x"})).status_code == 422
        assert (await p.api.put("/apps/etl/secrets/k", json={"value": "x"})).status_code == 409


async def test_tool_settings_are_checked_against_the_connector():
    async with Platform(["x"]) as p:
        await p.api.post("/apps", json={"app": "orders", "title": "订单"})
        config = order_flow(p.crm)
        config["graph"]["nodes"][1]["tool"] = "delete_order"
        config["env"]["secrets"] = []
        resp = await p.api.put("/apps/orders/draft", json={"config": config})
        found = {tuple(d["loc"][4:]): d["msg"] for d in resp.json()["detail"]}
        assert found == {("lookup", "tool"): "数据源「CRM」没有工具 delete_order"}

        config["graph"]["nodes"][1]["tool"] = "get_order"
        resp = await p.api.put("/apps/orders/draft", json={"config": config})
        found = {tuple(d["loc"][4:]): d["msg"] for d in resp.json()["detail"]}
        assert found == {
            ("lookup", "args", "token"): "没有声明密钥 crm_token，请先在应用的密钥里添加"
        }

        config = order_flow("ds_nope")
        resp = await p.api.put("/apps/orders/draft", json={"config": config})
        found = {tuple(d["loc"][4:]): d["msg"] for d in resp.json()["detail"]}
        assert found == {("lookup", "datasource"): "没有数据源 ds_nope"}


def call(tool: str, **args) -> str:
    return json.dumps({"action": "call", "tool": tool, "args": args})


def answer(text: str) -> str:
    return json.dumps({"action": "answer", "text": text})


async def test_datasource_qa_agent_calls_tools_and_answers():
    replies = [
        call("t1"),
        call("t2", schema="sales"),
        json.dumps({"action": "read_skill", "name": "glossary"}),
        answer("sales 库里有 customers 和 orders 两张表。"),
    ]
    async with Platform(replies) as p:
        await p.skills.create("glossary", SkillContent("销售术语", "GMV 是成交总额。"))
        await p.skills.publish("glossary", "第一版", None)
        config = copy.deepcopy(builtin.TEMPLATES["datasource-qa"].config)
        config["graph"]["nodes"][1]["skills"] = [{"name": "glossary", "version": 1}]
        await p.new_app("ask-data", config)
        run = await p.run("ask-data", {"question": "有哪些表？", "datasource": p.shop})
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"answer": "sales 库里有 customers 和 orders 两张表。"}

        system = p.gateway.requests[0]["body"]["messages"][0]["content"]
        assert "t1：数据源「示例库」的 list_schemas" in system
        assert "glossary：销售术语" in system
        assert "GMV" not in system  # Loaded only when asked for.
        assert '"schemas": ["sales"]' in p.gateway.requests[1]["body"]["messages"][-1]["content"]
        assert "GMV 是成交总额" in p.gateway.requests[3]["body"]["messages"][-1]["content"]

        flow = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()
        agent = next(s for s in flow["steps"] if s["node"] == "agent")
        steps = agent["output"]["vars"]["agent"]["steps"]
        assert [(s["tool"], s["args"]) for s in steps] == [
            ("list_schemas", {}),
            ("list_tables", {"schema": "sales"}),
            ("read_skill", {"name": "glossary"}),
        ]


async def test_agent_stops_at_its_step_limit():
    async with Platform([call("t1"), call("t1"), call("t1")]) as p:
        config = copy.deepcopy(builtin.TEMPLATES["datasource-qa"].config)
        config["graph"]["nodes"][1]["max_steps"] = 1
        await p.new_app("loops", config)
        run = await p.run("loops", {"question": "?", "datasource": p.shop})
        assert run["status"] == "error"
        assert run["error"] == "节点「查数据并回答」(agent)：调用了 1 次工具仍没有给出回答"
        nudge = p.gateway.requests[2]["body"]["messages"][-1]["content"]
        assert "已经达到步数上限" in nudge


async def test_trying_one_node():
    async with Platform(["这是回答", json.dumps({"class": "product"})]) as p:
        await p.api.post(
            "/apps",
            json={"app": "support", "title": "客服", "from": {"template": "intent-routing"}},
        )
        tried = await p.api.post(
            "/apps/support/nodes/answer/test", json={"values": {"start.question": "多少钱"}}
        )
        body = tried.json()
        assert (body["status"], body["outputs"], body["branch"]) == (
            "done",
            {"text": "这是回答"},
            None,
        )
        assert body["rendered"] == {"prompt": "多少钱"}
        assert body["duration_ms"] >= 0
        routed = (await p.api.post("/apps/support/nodes/route/test", json={"values": {}})).json()
        assert (routed["outputs"]["class"], routed["branch"]) == ("product", "product")
        assert (await p.api.post("/apps/support/nodes/review/test", json={})).status_code == 422
        assert (await p.api.post("/apps/support/nodes/nope/test", json={})).status_code == 404

        # An unsaved configuration, with a problem in the node tried.
        config = copy.deepcopy(builtin.TEMPLATES["intent-routing"].config)
        config["graph"]["nodes"][4]["model"] = "gpt-9"
        bad = await p.api.post("/apps/support/nodes/answer/test", json={"config": config})
        assert bad.status_code == 422
        assert bad.json()["detail"][0]["loc"][-2:] == ["answer", "model"]

        await p.new_app("orders", order_flow(p.crm))
        await p.api.put("/apps/orders/secrets/crm_token", json={"value": "s3cret"})
        tool = (
            await p.api.post(
                "/apps/orders/nodes/lookup/test", json={"values": {"start.order_id": "A9"}}
            )
        ).json()
        assert tool["outputs"] == {"result": {"order_id": "A9", "status": "shipped"}}
        assert tool["rendered"] == {"args.order_id": "A9", "args.token": "******"}
        failing = (
            await p.api.post(
                "/apps/orders/nodes/lookup/test", json={"values": {"start.order_id": "boom"}}
            )
        ).json()
        assert failing["status"] == "error"
        assert failing["error"]["type"] == "tool_error"


async def test_export_and_import():
    async with Platform(["x"]) as p:
        await p.skills.create("polite", SkillContent("礼貌", "先称呼您。"))
        await p.skills.publish("polite", "第一版", None)
        config = order_flow(p.crm)
        config["graph"]["nodes"].insert(
            2,
            {
                "id": "say",
                "type": "llm",
                "title": "说明",
                "model": "qwen",
                "skills": [{"name": "polite", "version": 1}],
                "prompt": "{{lookup.result}}",
            },
        )
        config["graph"]["edges"] = [
            {"from": "start", "to": "lookup"},
            {"from": "lookup", "to": "say"},
            {"from": "say", "to": "end"},
        ]
        await p.new_app("orders", config)
        await p.api.put("/apps/orders/secrets/crm_token", json={"value": "s3cret"})

        exported = await p.api.get("/apps/orders/versions/2/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("text/yaml")
        assert 'filename="orders-v2.yaml"' in exported.headers["content-disposition"]
        assert "s3cret" not in exported.text
        data = yaml.safe_load(exported.text)
        assert data["format"] == "aep-app/1"
        assert data["exported_from"] == {"app": "orders", "version": 2}
        assert data["requires"] == {
            "skills": [{"name": "polite", "version": 1}],
            "datasources": [{"datasource_id": p.crm, "name": "CRM", "kind": "postgres"}],
            "secrets": ["crm_token"],
        }
        assert (await p.api.get("/apps/orders/versions/9/export")).status_code == 404
        assert (await p.api.get("/apps/etl/versions/1/export")).status_code == 409

        # Everything is here but the new app's secret: the file becomes version 1.
        made = await p.api.post("/apps/import", json={"app": "orders-2", "yaml": exported.text})
        assert made.status_code == 201, made.text
        assert made.json()["problems"] == [
            {"kind": "secret", "name": "crm_token", "message": "密钥没有值，请在应用设置里填写"}
        ]
        detail = made.json()["app"]
        assert (detail["title"], detail["current"]["note"], detail["draft"]) == (
            "orders",
            "从文件导入",
            None,
        )

        # On another platform the skill, data source and model are missing.
        data["config"]["graph"]["nodes"][2]["skills"] = [{"name": "polite", "version": 7}]
        data["config"]["graph"]["nodes"][2]["model"] = "claude-bedrock"
        data["config"]["graph"]["nodes"][1]["datasource"] = "ds_elsewhere"
        data["requires"]["datasources"][0]["datasource_id"] = "ds_elsewhere"
        other = yaml.safe_dump(data, allow_unicode=True)
        made = await p.api.post(
            "/apps/import", json={"app": "orders-3", "title": "副本", "yaml": other}
        )
        assert made.status_code == 201, made.text
        kinds = [(x["kind"], x.get("name")) for x in made.json()["problems"]]
        assert kinds == [
            ("skill", "polite"),
            ("datasource", "CRM"),
            ("model", "claude-bedrock"),
            ("secret", "crm_token"),
        ]
        detail = made.json()["app"]
        assert detail["current"]["note"] == "新建空白应用（导入的配置在草稿里）"
        assert detail["draft"]["config"]["graph"]["nodes"][1]["datasource"] == "ds_elsewhere"

        into = await p.api.post("/apps/orders/draft/import", json={"yaml": exported.text})
        assert into.status_code == 200
        assert into.json()["app"]["draft"] is not None

        assert (
            await p.api.post("/apps/import", json={"app": "orders-2", "yaml": exported.text})
        ).status_code == 409
        for text in ["not: [valid", "format: other/1\nconfig: {}", "- a\n- b"]:
            bad = await p.api.post("/apps/import", json={"app": "orders-4", "yaml": text})
            assert bad.status_code == 422
        broken = copy.deepcopy(data)
        broken["config"]["graph"]["edges"] = []
        bad = await p.api.post(
            "/apps/import",
            json={"app": "orders-4", "yaml": yaml.safe_dump(broken, allow_unicode=True)},
        )
        assert bad.status_code == 422
        assert (await p.api.get("/apps/orders-4")).status_code == 404
