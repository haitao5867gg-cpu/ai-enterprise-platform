"""Knowledge nodes through the same save/run/test/import APIs as tool nodes."""

import copy
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from aep.config import get_settings
from aep.datasources import MemoryDataSourceStore
from aep.knowledge.citations import IndexedHit, context
from aep.knowledge.store import MemoryKnowledgeStore
from aep.knowledge.worker import KnowledgeWorker
from aep.main import create_app
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import builtin
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from tests.conftest import SETTINGS, FakeGateway

POLICY = "消费者收到商品后三十天内可以申请退货，商品需要保持原有包装并附带购买凭证。"
OTHER = "数据库订单明细宽表应当关联客户维度，保留订单标识和城市字段，按天进行增量加载。"


def knowledge_flow(kb_ids: list[str], **settings) -> dict:
    kb = {
        "id": "kb",
        "type": "knowledge",
        "title": "检索政策",
        "knowledge_bases": kb_ids,
        "query": "{{start.question}}",
        "top_k": 5,
        "min_score": 0.3,
        **settings,
    }
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [{"key": "question", "label": "问题", "type": "text"}],
                },
                kb,
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [
                        {"key": key, "from": [f"{{{{kb.{key}}}}}"]} for key in ("hits", "context")
                    ],
                },
            ],
            "edges": [{"from": "start", "to": "kb"}, {"from": "kb", "to": "end"}],
        },
    }


class Platform:
    def __init__(self, tmp_path) -> None:
        self.settings = SETTINGS.model_copy(update={"knowledge_dir": tmp_path})
        self.gateway = FakeGateway(["三十天内可以申请退货。[1]"])
        self.knowledge = MemoryKnowledgeStore()
        self.sources = MemoryDataSourceStore()
        self.ctx = AppContext(self.gateway.client, self.settings, knowledge=self.knowledge)
        self.records = MemoryRunStore()
        self.checkpointer = InMemorySaver()
        self.service = RunService(self.records, self.checkpointer, default_apps(), self.ctx)
        self.catalog = AppCatalog(self.service, self.sources, self.settings)
        self.worker = KnowledgeWorker(self.knowledge, self.gateway.client, retry_delays=(0, 0))
        self.app = create_app()
        self.app.state.catalog = self.catalog
        self.app.state.run_service = self.service
        self.app.state.knowledge = self.knowledge
        self.app.state.knowledge_worker = self.worker
        self.app.state.llm_client = self.gateway.client
        self.app.dependency_overrides[get_settings] = lambda: self.settings

    async def seed(self, text: str = POLICY, name: str = "售后政策") -> str:
        made = await self.api.post("/knowledge-bases", json={"name": name})
        assert made.status_code == 201, made.text
        kb_id = made.json()["kb_id"]
        uploaded = await self.api.post(
            f"/knowledge-bases/{kb_id}/documents",
            files={"files": ("政策.md", ("# 退货期限\n\n" + text).encode())},
        )
        assert uploaded.status_code == 202, uploaded.text
        assert await self.worker.process_once()
        document = await self.knowledge.get_document(kb_id, uploaded.json()[0]["document_id"])
        assert document.status == "ready", document.error
        return kb_id

    async def publish(self, config: dict, name: str = "ask-policy") -> None:
        created = await self.api.post("/apps", json={"app": name, "title": name})
        assert created.status_code == 201, created.text
        saved = await self.api.put(f"/apps/{name}/draft", json={"config": config})
        assert saved.status_code == 200, saved.text
        published = await self.api.post(f"/apps/{name}/draft/publish", json={"note": "测试"})
        assert published.status_code == 200, published.text

    async def run(self, question: str = POLICY, name: str = "ask-policy") -> dict:
        made = await self.api.post("/runs", json={"app": name, "input": {"question": question}})
        assert made.status_code == 201, made.text
        await self.service.wait_idle()
        return (await self.api.get(f"/runs/{made.json()['run_id']}")).json()


@pytest.fixture
async def p(tmp_path):
    platform = Platform(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=platform.app), base_url="http://test"
    ) as client:
        platform.api = client
        yield platform
    await platform.service.wait_idle()
    await platform.gateway.client.close()


@pytest.mark.parametrize(
    ("setting", "value", "suffix"),
    [
        ("knowledge_bases", [], []),
        ("knowledge_bases", "bad", []),
        ("knowledge_bases", ["missing"], [0]),
        ("knowledge_bases", ["missing"] * 6, []),
        ("knowledge_bases", [4], [0]),
        ("query", "", []),
        ("query", None, []),
        ("query", "字" * 2001, []),
        ("query", "{{missing.text}}", []),
        ("query", "{{start.unknown}}", []),
        ("query", "{{secret.token}}", []),
        ("top_k", 0, []),
        ("top_k", 21, []),
        ("top_k", True, []),
        ("top_k", 1.5, []),
        ("min_score", -0.1, []),
        ("min_score", 1.1, []),
        ("min_score", True, []),
        ("min_score", "0.3", []),
    ],
)
async def test_save_checks_settings_at_the_exact_location(p, setting, value, suffix):
    kb = await p.knowledge.create("资料")
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    config = (
        knowledge_flow([kb.kb_id], **{setting: value})
        if setting != "knowledge_bases"
        else (knowledge_flow(value))
    )
    response = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert response.status_code == 422, response.text
    assert ["body", "config", "graph", "nodes", "kb", setting, *suffix] in [
        problem["loc"] for problem in response.json()["detail"]
    ]


async def test_save_checks_every_base_and_references_that_may_not_have_run(p):
    kb = await p.knowledge.create("资料")
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    config = knowledge_flow([kb.kb_id, "missing"])
    response = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert response.json()["detail"][0]["loc"] == [
        "body",
        "config",
        "graph",
        "nodes",
        "kb",
        "knowledge_bases",
        1,
    ]
    config = copy.deepcopy(builtin.TEMPLATES["intent-routing"].config)
    config["graph"]["nodes"][2] = {
        **knowledge_flow([kb.kb_id])["graph"]["nodes"][1],
        "id": "draft",
        "query": "{{answer.text}}",
    }
    config["graph"]["nodes"][3]["show"] = "{{draft.context}}"
    config["graph"]["nodes"][-1]["outputs"][0]["from"][0] = "{{draft.context}}"
    response = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert response.status_code == 422
    assert any("不一定运行过" in problem["msg"] for problem in response.json()["detail"])


@pytest.mark.parametrize(("top_k", "min_score"), [(1, 0), (20, 1)])
async def test_setting_bounds_are_accepted(p, top_k, min_score):
    bases = [await p.knowledge.create(str(i)) for i in range(5)]
    await p.publish(
        knowledge_flow(
            [b.kb_id for b in bases], query="字" * 2000, top_k=top_k, min_score=min_score
        )
    )


async def test_knowledge_qa_template_starts_as_draft_then_runs_with_mock_embeddings(p):
    templates = (await p.api.get("/app-templates")).json()
    template = next(t for t in templates if t["id"] == "knowledge-qa")
    assert template["needs"] == ["knowledge_base"]
    made = await p.api.post(
        "/apps",
        json={"app": "ask-policy", "title": "政策问答", "from": {"template": "knowledge-qa"}},
    )
    assert made.status_code == 201, made.text  # No bases exist yet.
    detail = made.json()
    assert detail["current"]["version"] == 1
    assert detail["current"]["config"] == builtin.blank()
    assert detail["draft"]["config"] == template["config"]
    assert detail["options"]["knowledge_bases"] == []
    assert (
        await p.api.put("/apps/ask-policy/draft", json={"config": template["config"]})
    ).status_code == 422

    kb_id = await p.seed()
    config = detail["draft"]["config"]
    config["graph"]["nodes"][1]["knowledge_bases"] = [kb_id]
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 200, saved.text
    assert saved.json()["options"]["knowledge_bases"] == [
        {"kb_id": kb_id, "name": "售后政策", "document_count": 1}
    ]
    assert (
        await p.api.post("/apps/ask-policy/draft/publish", json={"note": "选择资料"})
    ).status_code == 200
    run = await p.run()
    assert run["status"] == "done", run["error"]
    assert run["result"] == {"answer": "三十天内可以申请退货。[1]"}
    [source] = run["sources"]
    assert source["index"] == 1 and source["text"] == POLICY
    assert (source["node"], source["node_title"]) == ("kb", "检索资料")
    assert source["cited"] is True
    prompt = p.gateway.requests[-1]["body"]["messages"][-1]["content"]
    assert f"[1]《政策.md》退货期限\n{POLICY}" in prompt
    assert all("embeddings" not in request["url"] for request in p.gateway.requests)
    assert all("sources" not in r for r in (await p.api.get("/runs")).json())


async def test_a_source_shown_for_a_decision_is_cited_while_it_waits(p):
    kb_id = await p.seed()
    config = knowledge_flow([kb_id])
    nodes = config["graph"]["nodes"]
    nodes[2:2] = [
        {
            "id": "answer",
            "type": "llm",
            "title": "回答",
            "prompt": "{{kb.context}}",
            "output": {"kind": "text"},
            "quiet": True,
        },
        {
            "id": "review",
            "type": "human",
            "title": "确认",
            "mode": "approval",
            "prompt": "请确认",
            "show": "{{answer.text}}",
            "options": [{"key": "approve", "label": "确认"}, {"key": "reject", "label": "驳回"}],
        },
    ]
    nodes[-1]["outputs"] = [{"key": "hits", "from": ["{{kb.hits}}"]}]
    config["graph"]["edges"] = [
        {"from": "start", "to": "kb"},
        {"from": "kb", "to": "answer"},
        {"from": "answer", "to": "review"},
        {"from": "review", "to": "end", "branch": "approve"},
        {"from": "review", "to": "answer", "branch": "reject"},
    ]
    await p.publish(config)
    run = await p.run()
    assert run["status"] == "waiting", run["error"]
    # Only the approver's view cites it: the quiet answer is not in the run process.
    assert [s["cited"] for s in run["sources"]] == [True]

    resumed = await p.api.post(f"/runs/{run['run_id']}/resume", json={"decision": "approve"})
    assert resumed.status_code == 202, resumed.text
    await p.service.wait_idle()
    done = (await p.api.get(f"/runs/{run['run_id']}")).json()
    assert done["status"] == "done", done["error"]
    assert [s["cited"] for s in done["sources"]] == [False]


async def test_node_outputs_context_empty_query_and_single_node_tests(p):
    kb_id = await p.seed()
    await p.publish(knowledge_flow([kb_id]))
    run = await p.run()
    [hit] = run["result"]["hits"]
    assert hit["index"] == 1 and hit["score"] == pytest.approx(1)
    assert run["result"]["context"] == f"[1]《政策.md》退货期限\n{POLICY}"
    # The result is the hits and their context passed through, which cite nothing.
    assert run["sources"] == [{**hit, "node": "kb", "node_title": "检索政策", "cited": False}]
    flow = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()
    assert next(n for n in flow["nodes"] if n["id"] == "kb")["kind"] == "tool"
    kb_step = next(s for s in flow["steps"] if s["node"] == "kb")
    assert set(kb_step["output"]["vars"]["kb"]) == {"hits", "context"}
    assert kb_step["output"]["citations"] == {hit["chunk_id"]: 1}

    for _ in range(2):
        tried = await p.api.post(
            "/apps/ask-policy/nodes/kb/test", json={"values": {"start.question": POLICY}}
        )
        assert tried.status_code == 200, tried.text
        body = tried.json()
        assert body["outputs"] == run["result"] and body["status"] == "done"
        assert body["rendered"] == {"query": POLICY}
    empty = await p.run("   ")
    assert empty["status"] == "done"
    assert empty["result"] == {"hits": [], "context": ""} and empty["sources"] == []
    unmatched = await p.run("An unrelated question with no matching text")
    assert unmatched["result"] == {"hits": [], "context": ""} and unmatched["sources"] == []
    await p.knowledge.delete(kb_id)
    # An empty rendered question does not contact even a base deleted since publishing.
    assert (await p.run(""))["result"] == {"hits": [], "context": ""}


@pytest.mark.parametrize("then", ["stop", "default", "branch"])
@pytest.mark.parametrize("failure", ["missing_base", "embedding"])
async def test_retrieval_failure_uses_existing_on_error(p, monkeypatch, then, failure):
    kb = await p.knowledge.create("资料")
    on_error = {"then": then, "retry": {"max": 1, "interval_seconds": 0}}
    if then == "default":
        on_error["default"] = {"hits": [], "context": "暂时无法检索"}
    config = knowledge_flow([kb.kb_id], on_error=on_error)
    if then != "stop":
        config["graph"]["nodes"][-1]["outputs"].extend(
            {"key": key, "from": [f"{{{{kb.{key}}}}}"]} for key in ("error_type", "error_message")
        )
    if then == "branch":
        config["graph"]["edges"].append({"from": "kb", "to": "end", "branch": "error"})
    await p.publish(config)
    if failure == "missing_base":
        await p.knowledge.delete(kb.kb_id)
    else:
        mock_embed = AsyncMock(side_effect=ValueError("向量维度不对"))
        monkeypatch.setattr("aep.llm.embed", mock_embed)
    run = await p.run()
    assert run["sources"] == []
    if then == "stop":
        assert run["status"] == "error"
        assert "节点「检索政策」(kb)：知识库检索失败" in run["error"]
    else:
        assert run["status"] == "done", run["error"]
        assert run["result"]["error_type"] == "knowledge_error"
        assert "知识库检索失败" in run["result"]["error_message"]
        if then == "default":
            assert run["result"]["context"] == "暂时无法检索"
        else:
            step = next(
                s for s in (await p.service.flow(run["run_id"]))["steps"] if s["node"] == "kb"
            )
            assert step["output"]["branch"] == {"kb": "error"}
    if failure == "embedding":
        assert mock_embed.await_count == 2


async def test_test_node_reports_failure_without_applying_on_error_and_renders_literal_query(
    p, monkeypatch
):
    kb = await p.knowledge.create("资料")
    await p.publish(
        knowledge_flow(
            [kb.kb_id],
            query="退货期限",
            on_error={"then": "default", "default": {"context": "备用"}, "retry": {"max": 3}},
        )
    )
    failing = AsyncMock(side_effect=ValueError("网关失败"))
    monkeypatch.setattr("aep.runtime.flow.nodes.search", failing)
    result = (await p.api.post("/apps/ask-policy/nodes/kb/test", json={})).json()
    assert result["status"] == "error" and result["outputs"] is None
    assert result["error"]["type"] == "knowledge_error"
    assert result["rendered"] == {"query": "退货期限"}
    assert failing.await_count == 1
    assert await p.service.recent() == []


@pytest.mark.parametrize("second_k", [1, 2])
async def test_sources_keep_shared_numbers_and_latest_node_across_pause_and_restart(p, second_k):
    kb_id = await p.seed(POLICY + "\n\n# 建表约定\n\n" + OTHER)
    config = knowledge_flow([kb_id], top_k=1)
    second = {
        **config["graph"]["nodes"][1],
        "id": "kb2",
        "title": "再检索",
        "query": OTHER,
        "top_k": second_k,
        "min_score": 0,
    }
    review = {
        "id": "review",
        "type": "human",
        "title": "继续",
        "prompt": "继续？",
        "options": [{"key": "approve", "label": "继续"}, {"key": "reject", "label": "重查"}],
    }
    config["graph"]["nodes"][2:2] = [review, second]
    config["graph"]["nodes"][-1]["outputs"] = [{"key": "hits", "from": ["{{kb2.hits}}"]}]
    config["graph"]["edges"] = [
        {"from": "start", "to": "kb"},
        {"from": "kb", "to": "review"},
        {"from": "review", "branch": "approve", "to": "kb2"},
        {"from": "review", "branch": "reject", "to": "kb"},
        {"from": "kb2", "to": "end"},
    ]
    await p.publish(config)
    waiting = await p.run()
    assert waiting["status"] == "waiting"
    [first] = waiting["sources"]
    assert first["index"] == 1
    # Run the first node again, then reconstruct the service on the persisted stores.
    await p.service.resume(waiting["run_id"], "reject", None)
    await p.service.wait_idle()
    assert (await p.service.get(waiting["run_id"])).sources == [first]
    old = p.service
    p.service = RunService(
        p.records, p.checkpointer, default_apps(), p.ctx, versions=old.versions, flows=old.flows
    )
    p.app.state.run_service = p.service
    await p.service.resume(waiting["run_id"], "approve", None)
    await p.service.wait_idle()
    run = (await p.api.get(f"/runs/{waiting['run_id']}")).json()
    assert run["status"] == "done", run["error"]
    assert [h["index"] for h in run["result"]["hits"]] == ([2] if second_k == 1 else [2, 1])
    assert [s["index"] for s in run["sources"]] == [1, 2]
    assert run["sources"][0]["chunk_id"] == first["chunk_id"]
    if second_k == 2:
        assert all((s["node"], s["node_title"]) == ("kb2", "再检索") for s in run["sources"])
    else:
        assert run["sources"][0] == first  # Earlier hits stay even when no longer returned.
        assert run["sources"][1]["node"] == "kb2"
    # A separate run starts its own numbering from 1.
    again = await p.run()
    assert again["sources"][0]["index"] == 1


async def test_export_import_missing_base_becomes_draft_and_cannot_save_until_fixed(p):
    kb = await p.knowledge.create("政策")
    await p.publish(knowledge_flow([kb.kb_id]))
    exported = await p.api.get("/apps/ask-policy/versions/2/export")
    data = yaml.safe_load(exported.text)
    assert data["requires"]["knowledge_bases"] == [{"kb_id": kb.kb_id, "name": "政策"}]
    imported = await p.api.post("/apps/import", json={"app": "copy-ok", "yaml": exported.text})
    assert imported.status_code == 201 and imported.json()["problems"] == []
    assert imported.json()["app"]["draft"] is None
    await p.knowledge.delete(kb.kb_id)
    imported = await p.api.post("/apps/import", json={"app": "copy-missing", "yaml": exported.text})
    assert imported.status_code == 201, imported.text
    problem = {
        "kind": "knowledge_base",
        "kb_id": kb.kb_id,
        "name": "政策",
        "message": "没有这个知识库，请在节点里重新选择",
    }
    assert imported.json()["problems"] == [problem]
    detail = imported.json()["app"]
    assert detail["current"]["config"] == builtin.blank()
    assert detail["draft"]["config"] == data["config"]
    assert (
        await p.api.put("/apps/copy-missing/draft", json={"config": data["config"]})
    ).status_code == 422
    into = await p.api.post("/apps/copy-ok/draft/import", json={"yaml": exported.text})
    assert into.status_code == 200 and into.json()["problems"] == [problem]


async def test_delete_only_checks_current_versions_including_etl_and_archived_flows(p):
    kb = await p.knowledge.create("政策")
    await p.publish(knowledge_flow([kb.kb_id]), "first-app")
    await p.publish(knowledge_flow([kb.kb_id]), "second-app")
    params = {"knowledge_bases": [kb.kb_id]}
    saved = await p.api.put(
        "/apps/etl/draft", json={"config": {"nodes": {"plan": {"params": params}}}}
    )
    assert saved.status_code == 200, saved.text
    # Publish the ETL fixture directly; its eval gate is covered by test_apps.
    await p.service.versions.publish("etl", "参考政策", None)
    await p.api.delete("/apps/second-app")
    response = await p.api.delete(f"/knowledge-bases/{kb.kb_id}")
    assert response.status_code == 409
    assert all(app in response.json()["detail"] for app in ("first-app", "second-app", "etl"))
    assert await p.knowledge.get(kb.kb_id) is not None
    # Old versions and drafts are retained but do not block deletion.
    for name in ("first-app", "second-app"):
        await p.service.versions.save_draft(name, builtin.blank(), None)
        await p.service.versions.publish(name, "移除知识库", None)
        await p.service.versions.save_draft(name, knowledge_flow([kb.kb_id]), None)
    await p.catalog.save_draft("etl", {"nodes": {}}, None)
    await p.service.versions.publish("etl", "移除知识库", None)
    assert (await p.api.delete(f"/knowledge-bases/{kb.kb_id}")).status_code == 204


async def test_node_type_contract_and_runs_without_retrieval_have_no_sources(p):
    types = (await p.api.get("/node-types")).json()
    kind = next(t for t in types if t["type"] == "knowledge")
    assert (kind["label"], kind["category"], kind["error_handling"]) == ("知识检索", "tool", True)
    assert kind["defaults"] == {
        "knowledge_bases": [],
        "query": "",
        "top_k": 5,
        "min_score": 0.3,
        "retrieval": {"mode": "hybrid", "vector_weight": 0.7, "rerank": None},
    }
    assert [o["key"] for o in kind["outputs"]] == ["hits", "context"]
    await p.publish(builtin.blank())
    assert (await p.run())["sources"] == []


@pytest.mark.parametrize(
    ("key", "value", "suffix"),
    [
        ("knowledge_bases", "missing", []),
        ("knowledge_bases", [1], []),
        ("knowledge_bases", ["missing"], [0]),
        ("knowledge_bases", [""], [0]),
        ("knowledge_top_k", 0, []),
        ("knowledge_top_k", 21, []),
        ("knowledge_top_k", True, []),
        ("knowledge_top_k", 1.5, []),
    ],
)
async def test_etl_knowledge_setting_validation(p, key, value, suffix):
    response = await p.api.put(
        "/apps/etl/draft", json={"config": {"nodes": {"plan": {"params": {key: value}}}}}
    )
    assert response.status_code == 422, response.text
    assert ["body", "config", "nodes", "plan", "params", key, *suffix] in [
        problem["loc"] for problem in response.json()["detail"]
    ]


async def test_etl_knowledge_setting_contract_and_accepted_values(p):
    kb = await p.knowledge.create("建表约定")
    for ids, count in (([], 1), ([kb.kb_id], 20)):
        response = await p.api.put(
            "/apps/etl/draft",
            json={
                "config": {
                    "nodes": {
                        "plan": {"params": {"knowledge_bases": ids, "knowledge_top_k": count}}
                    }
                }
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["draft"]["config"]["nodes"]["plan"]["params"] == {
            "max_attempts": 3,
            "knowledge_bases": ids,
            "knowledge_top_k": count,
            "knowledge_mode": "hybrid",
        }
    detail = response.json()
    settings = next(n for n in detail["nodes"] if n["id"] == "plan")["settings"]
    assert next(s for s in settings if s["key"] == "knowledge_bases") == {
        "key": "knowledge_bases",
        "label": "参考知识库",
        "type": "knowledge_bases",
        "help": "出方案前按需求检索这些知识库，方案里标出依据",
    }
    assert detail["current"]["config"]["nodes"]["plan"]["params"]["knowledge_bases"] == []
    assert detail["current"]["config"]["nodes"]["plan"]["params"]["knowledge_top_k"] == 5
    assert detail["options"]["knowledge_bases"] == [
        {"kb_id": kb.kb_id, "name": "建表约定", "document_count": 0}
    ]


def test_context_format_with_headings_pages_and_multiple_hits():
    common = {
        "kb_id": "kb",
        "kb_name": "政策",
        "document_id": "doc",
        "document_name": "退货.pdf",
        "position": 0,
        "score": 0.9,
    }
    hits = [
        IndexedHit(
            **common, chunk_id="a", text="第一段", heading="第三章 退货 > 3.2 期限", page=5, index=1
        ),
        IndexedHit(
            **{**common, "document_name": "说明.txt"},
            chunk_id="b",
            text="第二段",
            heading="",
            page=None,
            index=2,
        ),
    ]
    assert (
        context(hits)
        == "[1]《退货.pdf》第三章 退货 > 3.2 期限（第 5 页）\n第一段\n\n[2]《说明.txt》\n第二段"
    )
