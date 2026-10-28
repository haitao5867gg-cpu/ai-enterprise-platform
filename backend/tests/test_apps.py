"""App versions: drafts, the eval gate, publishing, rollback, and runs keeping their version."""

import asyncio
import copy

import httpx
import pytest
import yaml
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from aep.datasources import MemoryDataSourceStore
from aep.main import app
from aep.runtime import catalog, human
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow.builtin import HELLO
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import CommentRequired, RunService
from aep.runtime.store import MemoryRunStore
from aep.runtime.versions import DraftChanged, MemoryAppVersionStore
from aep.skills import MemorySkillStore, SkillContent
from aep.tools.mcp import McpTools
from tests import fake_connector
from tests.conftest import SETTINGS, FakeGateway
from tests.test_etl import PYSPARK_BODY, plan, reply

CASE = {
    "id": "order-facts",
    "request": "做一张订单明细宽表",
    "expect": {"sources": ["sales.orders"], "columns": ["order_id", "city"]},
}


class Platform:
    """One process: a service, its catalog and an API client."""

    def __init__(self, replies, tmp_path, versions=None, skills=None, **settings) -> None:
        cases = tmp_path / "etl" / "cases.yaml"
        cases.parent.mkdir(parents=True, exist_ok=True)
        cases.write_text(yaml.safe_dump([CASE], allow_unicode=True))
        self.settings = SETTINGS.model_copy(update={"evals_dir": str(tmp_path), **settings})
        self.gateway = FakeGateway(replies)
        self.sources = MemoryDataSourceStore()
        server = fake_connector.build()
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            default_apps(),
            AppContext(
                self.gateway.client,
                self.settings,
                datasources=self.sources,
                tools=McpTools(5, resolve=lambda url: server),
            ),
            versions=versions or MemoryAppVersionStore(),
            skills=skills or MemorySkillStore(),
        )
        self.catalog = AppCatalog(self.service, self.sources, self.settings, models=self.models)

    async def models(self) -> list[str]:
        return ["mock", "qwen", "company"]

    async def __aenter__(self) -> "Platform":
        await self.sources.create("示例库", "postgres", fake_connector.URL)
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

    async def settle(self) -> None:
        await self.catalog.wait_idle()
        await self.service.wait_idle()


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(catalog, "POLL_SECONDS", 0.01)


def models_used(gateway: FakeGateway) -> list[str]:
    return [r["body"]["model"] for r in gateway.requests]


def hello_config(model=None, skills=()) -> dict:
    config = copy.deepcopy(HELLO.config)
    draft = next(n for n in config["graph"]["nodes"] if n["id"] == "draft")
    draft |= {"model": model, "skills": list(skills)}
    return config


async def test_apps_list_and_detail(tmp_path):
    async with Platform(["你好"], tmp_path) as p:
        listed = (await p.api.get("/apps")).json()
        assert [(a["app"], a["current_version"], a["has_eval_set"]) for a in listed] == [
            ("etl", 1, True),
            ("hello", 1, False),
        ]

        detail = (await p.api.get("/apps/etl")).json()
        assert detail["title"] == "自然语言 ETL 工作流"
        assert "Skill" in detail["skills_text"]
        nodes = {n["id"]: n for n in detail["nodes"]}
        assert [s["key"] for s in nodes["plan"]["settings"]] == ["model", "skills", "max_attempts"]
        assert nodes["plan"]["settings"][2] | {"help": ""} == {
            "key": "max_attempts",
            "label": "最多生成次数",
            "type": "integer",
            "min": 1,
            "max": 5,
            "help": "",
        }
        assert nodes["review"]["settings"] == []
        assert nodes["plan"]["design"]["prompt"].startswith("你是资深数据工程师")
        assert detail["current"]["status"] == "current"
        assert detail["current"]["config"]["nodes"]["plan"] == {
            "model": None,
            "skills": [{"name": "etl-plan", "version": 1}],
            "params": {"max_attempts": 3},
        }
        assert detail["current"]["note"] == "平台默认配置"
        assert detail["draft"] is None
        assert detail["options"]["models"] == ["mock", "qwen", "company"]
        assert detail["options"]["default_model"] == "mock"
        assert [s["name"] for s in detail["options"]["skills"]] == ["etl-codegen", "etl-plan"]
        assert (await p.api.get("/apps/nope")).status_code == 404


async def test_draft_is_checked_against_the_app_options(tmp_path):
    async with Platform(["你好"], tmp_path) as p:
        bad = {
            "nodes": {
                "plan": {"model": "gpt-9", "params": {"max_attempts": 9, "color": "red"}},
                "codegen": {"skills": [{"name": "etl-plan", "version": 7}]},
                "review": {"model": "qwen"},
            }
        }
        resp = await p.api.put("/apps/etl/draft", json={"config": bad})
        assert resp.status_code == 422
        problems = {tuple(d["loc"]): d["msg"] for d in resp.json()["detail"]}
        assert problems == {
            ("body", "config", "nodes", "review"): "节点 review 没有可调设置",
            ("body", "config", "nodes", "plan", "model"): "模型网关上没有 gpt-9",
            ("body", "config", "nodes", "plan", "params", "max_attempts"): "最多生成次数不能大于 5",
            ("body", "config", "nodes", "plan", "params", "color"): "没有参数 color",
            (
                "body",
                "config",
                "nodes",
                "codegen",
                "skills",
                0,
            ): "Skill etl-plan 没有已发布的版本 7",
        }

        # Nodes and params left out keep their defaults.
        ok = {"nodes": {"plan": {"model": "qwen", "params": {"max_attempts": 2}}}}
        resp = await p.api.put("/apps/etl/draft", json={"config": ok, "actor": "张三"})
        assert resp.status_code == 200
        draft = resp.json()["draft"]
        assert (draft["status"], draft["version"], draft["eval"]) == ("draft", None, None)
        assert draft["created_by"] == "张三"
        assert draft["config"]["nodes"]["plan"] == {
            "model": "qwen",
            "skills": [{"name": "etl-plan", "version": 1}],
            "params": {"max_attempts": 2},
        }
        assert draft["config"]["nodes"]["codegen"]["params"] == {"max_attempts": 3}

        assert (await p.api.delete("/apps/etl/draft")).status_code == 204
        assert (await p.api.get("/apps/etl")).json()["draft"] is None
        # Discarding again is fine: there is still no draft.
        assert (await p.api.delete("/apps/etl/draft")).status_code == 204


async def test_publish_and_roll_back_without_an_eval_set(tmp_path):
    replies = ["第一版", "第二版", "第三版", "改过的第二版"]
    async with Platform(replies, tmp_path) as p:
        await p.service.skills.create("polite", SkillContent("礼貌用语", "回答时先称呼“您”。"))
        await p.service.skills.publish("polite", "第一版", None)
        config = hello_config("qwen", [{"name": "polite", "version": 1}])
        await p.api.put("/apps/hello/draft", json={"config": config})
        published = await p.api.post(
            "/apps/hello/draft/publish", json={"note": "换模型、加 Skill", "actor": "张三"}
        )
        assert published.status_code == 200
        assert published.json()["current_version"] == 2
        assert published.json()["draft"] is None

        # One run at a time, so the gateway sees the calls in order.
        before = await p.service.create("hello", {"text": "你好"}, app_version=1)
        await p.settle()
        on_v2 = await p.service.create("hello", {"text": "你好"})
        await p.settle()
        assert (before.app_version, on_v2.app_version) == (1, 2)
        assert models_used(p.gateway) == ["mock", "qwen"]
        system = p.gateway.requests[1]["body"]["messages"][0]["content"]
        assert system == "回答时先称呼“您”。\n\n你是一个乐于助人的助手，用中文简洁地回答。"

        # Roll back; the run on version 2 keeps version 2 when it goes on.
        rolled = await p.api.post("/apps/hello/versions/1/activate", json={"actor": "张三"})
        assert rolled.json()["current_version"] == 1
        after = await p.service.create("hello", {"text": "你好"})
        await p.settle()
        await p.service.resume(on_v2.run_id, "reject", "再短一点")
        await p.settle()
        assert after.app_version == 1
        assert models_used(p.gateway) == ["mock", "qwen", "mock", "qwen"]

        run = (await p.api.get(f"/runs/{on_v2.run_id}")).json()
        assert run["app_version"] == 2
        listed = (await p.api.get("/runs")).json()
        assert {r["app_version"] for r in listed} == {1, 2}
        flow = (await p.api.get(f"/runs/{on_v2.run_id}/flow")).json()
        draft_node = next(n for n in flow["nodes"] if n["id"] == "draft")
        assert draft_node["model"] == "qwen"
        assert draft_node["skills"] == [{"name": "polite", "version": 1}]
        assert draft_node["design"]["prompt"] == system

        versions = (await p.api.get("/apps/hello/versions")).json()
        assert [(v["version"], v["status"], v["note"]) for v in versions] == [
            (2, "previous", "换模型、加 Skill"),
            (1, "current", "平台默认配置"),
        ]
        missing = await p.api.post("/apps/hello/versions/9/activate", json={})
        assert missing.status_code == 404
        unknown = await p.api.post("/runs", json={"app": "hello", "app_version": 9})
        assert unknown.status_code == 404


async def test_draft_must_pass_the_eval_set_before_publishing(tmp_path):
    good = [reply(plan()), reply({"targets": {"order_facts": PYSPARK_BODY}})]
    async with Platform(good, tmp_path) as p:
        config = {"nodes": {"codegen": {"model": "company"}}}
        await p.api.put("/apps/etl/draft", json={"config": config})

        refused = await p.api.post("/apps/etl/draft/publish", json={"note": "试试"})
        assert refused.status_code == 409
        assert refused.json()["detail"] == "草稿还没有跑评测，通过后才能发布"

        started = await p.api.post("/apps/etl/draft/eval", json={})
        assert started.status_code == 202
        assert started.json()["draft"]["eval"]["status"] == "running"
        await p.settle()

        result = (await p.api.get("/apps/etl")).json()["draft"]["eval"]
        assert (result["status"], result["score"], result["threshold"]) == ("passed", 1.0, 0.8)
        [case] = result["cases"]
        assert (case["id"], case["status"], case["score"]) == ("order-facts", "done", 1.0)
        assert models_used(p.gateway) == ["mock", "company"]
        run = await p.service.get(case["run_id"])
        assert run.record.app_version is None  # a draft's run
        assert run.result["approvals"][0]["actor"] == "aep-eval"

        published = await p.api.post("/apps/etl/draft/publish", json={"note": "写代码换模型"})
        assert published.status_code == 200
        current = published.json()["current"]
        assert (current["version"], current["eval"]["status"]) == (2, "passed")


async def test_a_failing_draft_cannot_be_published(tmp_path):
    async with Platform([reply({"summary": "缺字段"})], tmp_path) as p:
        one_try = {"nodes": {"plan": {"params": {"max_attempts": 1}}}}
        await p.api.put("/apps/etl/draft", json={"config": one_try})
        first, again = await asyncio.gather(
            p.api.post("/apps/etl/draft/eval", json={}),
            p.api.post("/apps/etl/draft/eval", json={}),
        )
        assert sorted([first.status_code, again.status_code]) == [202, 409]
        await p.settle()

        result = (await p.api.get("/apps/etl")).json()["draft"]["eval"]
        assert result["status"] == "failed"
        assert result["score"] == 0.0
        assert result["cases"][0]["status"] == "error"
        assert len(p.gateway.requests) == 1  # max_attempts 1: the plan was not retried
        refused = await p.api.post("/apps/etl/draft/publish", json={"note": "试试"})
        assert refused.json()["detail"] == "草稿没有通过评测，通过后才能发布"

        # Saving the draft again drops its eval.
        await p.api.put("/apps/etl/draft", json={"config": {"nodes": {}}})
        assert (await p.api.get("/apps/etl")).json()["draft"]["eval"] is None

        no_set = await p.api.post("/apps/hello/draft/eval", json={})
        assert no_set.status_code == 409


async def test_a_missing_eval_set_file_keeps_the_gate_closed(tmp_path):
    async with Platform([reply(plan())], tmp_path, evals_dir=str(tmp_path / "nowhere")) as p:
        assert (await p.api.get("/apps/etl")).json()["has_eval_set"] is True
        await p.api.put("/apps/etl/draft", json={"config": {"nodes": {}}})
        started = await p.api.post("/apps/etl/draft/eval", json={})
        assert started.status_code == 409
        assert "AEP_EVALS_DIR" in started.json()["detail"]
        refused = await p.api.post("/apps/etl/draft/publish", json={"note": "试试"})
        assert refused.status_code == 409


async def test_only_the_evaluated_draft_is_published():
    store = MemoryAppVersionStore()
    await store.ensure("etl", {"nodes": {}})
    checked = await store.save_draft("etl", {"nodes": {}}, None)
    await store.save_draft("etl", {"nodes": {"plan": {"model": "qwen"}}}, None)
    with pytest.raises(DraftChanged):
        await store.publish("etl", "评测过的是旧草稿", None, checked.revision)
    assert await store.current("etl") == 1
    assert await store.draft("etl") is not None


async def test_an_eval_cut_off_by_a_restart_is_marked(tmp_path):
    versions = MemoryAppVersionStore()
    async with Platform([reply(plan())], tmp_path, versions=versions) as p:
        await p.service.ensure_versions("etl")
        draft = await versions.save_draft("etl", {"nodes": {}}, None)
        await versions.set_draft_eval("etl", draft.revision, {"status": "running", "cases": []})
    async with Platform([reply(plan())], tmp_path, versions=versions) as restarted:
        assert await restarted.catalog.recover() == 1
        result = (await restarted.api.get("/apps/etl")).json()["draft"]["eval"]
        assert result["status"] == "error"
        assert "中断" in result["error"]


async def test_an_option_can_require_a_comment():
    def build(ctx):
        def ask(state, config: RunnableConfig):
            answer = human.ask("上线吗？", {"type": "demo"}, comment_required=("reject",))
            return {"answer": answer}

        graph = StateGraph(dict)
        graph.add_node("ask", ask)
        graph.add_edge(START, "ask")
        graph.add_edge("ask", END)
        return graph

    service = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        {"demo": build},
        AppContext(FakeGateway().client, SETTINGS),
    )
    record = await service.create("demo", {})
    await service.wait_idle()
    view = await service.get(record.run_id)
    assert view.pending["comment_required"] == ["reject"]
    with pytest.raises(CommentRequired):
        await service.resume(record.run_id, "reject", "  ")
    await service.resume(record.run_id, "reject", "数据不对")
