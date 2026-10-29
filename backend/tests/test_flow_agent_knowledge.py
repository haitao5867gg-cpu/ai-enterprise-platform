"""Issue #76: agent knowledge tools through the save, run and asset APIs."""

import copy
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
import yaml
from mcp.server.mcpserver import MCPServer

from aep.knowledge.citations import IndexedHit, context
from aep.knowledge.search import search
from aep.runtime.flow import builtin
from aep.runtime.flow.nodes import MAX_TOOL_RESULT
from tests import fake_connector
from tests.test_flow_knowledge import OTHER, POLICY, knowledge_flow
from tests.test_flow_tools import Platform as ToolPlatform
from tests.test_flow_tools import answer, call
from tests.test_knowledge_search_api import INVALID

pytest_plugins = ("tests.test_flow_knowledge",)


def agent_flow(kb_ids: list[str], **settings) -> dict:
    config = copy.deepcopy(builtin.TEMPLATES["knowledge-agent"].config)
    config["graph"]["nodes"][1]["knowledge"].update(knowledge_bases=kb_ids, **settings)
    config["graph"]["nodes"][-1]["outputs"] += [
        {"key": key, "from": [f"{{{{agent.{key}}}}}"]} for key in ("steps", "hits")
    ]
    return config


@pytest.mark.parametrize("omit", [False, True])
async def test_agent_requires_a_datasource_tool_or_knowledge_at_tools(p, omit):
    config = agent_flow([])
    agent = config["graph"]["nodes"][1]
    if omit:
        agent.pop("knowledge")
    else:
        agent["knowledge"] = None
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 422, saved.text
    assert saved.json()["detail"] == [
        {
            "loc": ["body", "config", "graph", "nodes", "agent", "tools"],
            "msg": "至少选一个数据源工具或知识库",
            "type": "value_error",
        }
    ]


@pytest.mark.parametrize("knowledge", [False, [], "资料", 1])
async def test_agent_knowledge_must_be_an_object(p, knowledge):
    config = agent_flow([])
    config["graph"]["nodes"][1]["knowledge"] = knowledge
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 422, saved.text
    assert saved.json()["detail"][0]["loc"][-2:] == ["agent", "knowledge"]


@pytest.mark.parametrize(
    ("key", "value", "suffix"),
    [
        ("knowledge_bases", [], []),
        ("knowledge_bases", "bad", []),
        ("knowledge_bases", ["missing"], [0]),
        ("knowledge_bases", ["missing"] * 6, []),
        ("knowledge_bases", [4], [0]),
        ("knowledge_bases", [""], [0]),
        ("top_k", 0, []),
        ("top_k", 11, []),
        ("top_k", True, []),
        ("top_k", 1.5, []),
        ("top_k", "5", []),
        ("min_score", -0.1, []),
        ("min_score", 1.1, []),
        ("min_score", True, []),
        ("min_score", "0.3", []),
        ("retrieval", [], []),
        ("retrieval", "", []),
        ("retrieval", False, []),
        *[("retrieval", retrieval, loc) for retrieval, loc in INVALID],
    ],
)
async def test_agent_checks_knowledge_settings_at_nested_locations(p, key, value, suffix):
    kb = await p.knowledge.create("资料")
    config = agent_flow([kb.kb_id])
    config["graph"]["nodes"][1]["knowledge"][key] = value
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 422, saved.text
    assert ["body", "config", "graph", "nodes", "agent", "knowledge", key, *suffix] in [
        problem["loc"] for problem in saved.json()["detail"]
    ]


async def test_agent_checks_every_base_and_rerank_candidates_against_top_k(p):
    kb = await p.knowledge.create("资料")
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    config = agent_flow([kb.kb_id, "missing"])
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 422
    assert saved.json()["detail"][0]["loc"][-3:] == ["knowledge", "knowledge_bases", 1]
    config = agent_flow(
        [kb.kb_id], top_k=6, retrieval={"rerank": {"model": "mock-rerank", "candidates": 5}}
    )
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 422
    assert saved.json()["detail"][0]["loc"][-4:] == [
        "knowledge",
        "retrieval",
        "rerank",
        "candidates",
    ]


@pytest.mark.parametrize(("top_k", "min_score", "tool_count"), [(1, 0, 0), (10, 1, 10)])
async def test_agent_accepts_knowledge_and_tool_count_bounds(p, top_k, min_score, tool_count):
    bases = [await p.knowledge.create(str(i)) for i in range(5)]
    source = await p.sources.create("数据", "postgres", "http://connector.test/mcp")
    config = agent_flow([kb.kb_id for kb in bases], top_k=top_k, min_score=min_score)
    config["graph"]["nodes"][1]["tools"] = [
        {"datasource": source.datasource_id, "tools": None} for _ in range(tool_count)
    ]
    await p.publish(config)
    config["graph"]["nodes"][1]["tools"] = [
        {"datasource": source.datasource_id, "tools": None} for _ in range(11)
    ]
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 422
    assert saved.json()["detail"][0]["loc"][-2:] == ["agent", "tools"]


async def test_knowledge_agent_template_starts_as_draft_and_runs_without_datasource_tools(p):
    types = (await p.api.get("/node-types")).json()
    agent_type = next(t for t in types if t["type"] == "agent")
    assert agent_type["defaults"]["knowledge"] is None
    assert [o["key"] for o in agent_type["outputs"]] == ["text", "steps", "hits"]
    templates = (await p.api.get("/app-templates")).json()
    template = next(t for t in templates if t["id"] == "knowledge-agent")
    assert template["title"] == "知识库智能体"
    assert template["needs"] == ["knowledge_base"]
    made = await p.api.post(
        "/apps",
        json={"app": "ask-policy", "title": "政策问答", "from": {"template": "knowledge-agent"}},
    )
    assert made.status_code == 201, made.text
    detail = made.json()
    assert detail["current"]["version"] == 1
    assert detail["current"]["config"] == builtin.blank()
    assert detail["draft"]["config"] == template["config"]
    assert (
        await p.api.put("/apps/ask-policy/draft", json={"config": template["config"]})
    ).status_code == 422

    kb_id = await p.seed()
    await p.knowledge.update(kb_id, description="商品退换货的期限和凭证要求")
    config = detail["draft"]["config"]
    agent = config["graph"]["nodes"][1]
    assert agent["tools"] == [] and agent["max_steps"] == 6
    assert "先检索" in agent["task"] and "[n]" in agent["task"]
    agent["knowledge"]["knowledge_bases"] = [kb_id]
    saved = await p.api.put("/apps/ask-policy/draft", json={"config": config})
    assert saved.status_code == 200, saved.text
    assert (
        await p.api.post("/apps/ask-policy/draft/publish", json={"note": "选择知识库"})
    ).status_code == 200
    p.gateway.replies = [call("kb", query=POLICY), answer("三十天内可以申请退货。[1]")]
    run = await p.run()
    assert run["status"] == "done", run["error"]
    assert run["result"] == {"answer": "三十天内可以申请退货。[1]"}
    [source] = run["sources"]
    assert source["index"] == 1 and source["text"] == POLICY
    assert (source["node"], source["node_title"]) == ("agent", agent["title"])
    assert source["cited"] is True
    system = p.gateway.requests[0]["body"]["messages"][0]["content"]
    assert "kb：在知识库里检索资料" in system
    assert "「售后政策」：商品退换货的期限和凭证要求" in system
    assert "knowledge_bases 可选，不写查全部" in system
    assert "按资料前的 [n] 标出处；资料里没有的不要编" in system
    assert (
        f"[1]《政策.md》退货期限\n{POLICY}"
        in (p.gateway.requests[1]["body"]["messages"][-1]["content"])
    )
    flow = await p.service.flow(run["run_id"])
    outputs = next(s for s in flow["steps"] if s["node"] == "agent")["output"]
    [step] = outputs["vars"]["agent"]["steps"]
    assert step == {
        "tool": "search_knowledge",
        "datasource": None,
        "args": {"query": POLICY, "knowledge_bases": ["售后政策"]},
        "error": None,
        "hits": 1,
    }
    assert outputs["citations"] == {source["chunk_id"]: 1}
    assert all("embeddings" not in request["url"] for request in p.gateway.requests)


@pytest.mark.parametrize("has_datasource_tools", [False, True])
async def test_agent_knowledge_coexists_with_datasource_tools_or_an_empty_connector(
    has_datasource_tools,
):
    replies = [call("t1")] if has_datasource_tools else []
    replies += [call("kb", query=POLICY), answer("根据工具结果回答。")]
    servers = None if has_datasource_tools else {fake_connector.URL: MCPServer("empty")}
    async with ToolPlatform(replies, servers=servers) as p:
        kb = await p.service.knowledge.create("售后政策")
        config = agent_flow([kb.kb_id])
        agent = config["graph"]["nodes"][1]
        agent["tools"] = [
            {"datasource": p.shop, "tools": ["list_schemas"] if has_datasource_tools else None}
        ]
        await p.new_app("ask-mixed", config)
        run = await p.run("ask-mixed", {"question": POLICY})
        assert run["status"] == "done", run["error"]
        steps = run["result"]["steps"]
        assert len(steps) == (2 if has_datasource_tools else 1)
        assert steps[-1]["tool"] == "search_knowledge" and steps[-1]["hits"] == 0
        if has_datasource_tools:
            assert steps[0]["tool"] == "list_schemas"
            assert "hits" not in steps[0]
            system = p.gateway.requests[0]["body"]["messages"][0]["content"]
            assert "t1：数据源" in system and "kb：在知识库里检索资料" in system


async def test_agent_accumulates_shared_numbers_and_deduplicates_hits_across_searches(p):
    kb_id = await p.seed(POLICY + "\n\n# 建表约定\n\n" + OTHER)
    config = agent_flow([kb_id], top_k=1, min_score=0)
    config["graph"]["nodes"].insert(1, knowledge_flow([kb_id], top_k=1)["graph"]["nodes"][1])
    config["graph"]["nodes"][-1]["outputs"].append({"key": "earlier_hits", "from": ["{{kb.hits}}"]})
    config["graph"]["edges"] = [
        {"from": "start", "to": "kb"},
        {"from": "kb", "to": "agent"},
        {"from": "agent", "to": "end"},
    ]
    await p.publish(config)
    p.gateway.replies = [
        call("kb", query=OTHER),
        call("kb", query=POLICY),
        call("kb", query=OTHER),
        answer("按资料回答。[1][2]"),
    ]
    run = await p.run()
    assert run["status"] == "done", run["error"]
    [earlier] = run["result"]["earlier_hits"]
    assert earlier["index"] == 1
    hits = run["result"]["hits"]
    assert [(h["index"], h["text"]) for h in hits] == [(1, POLICY), (2, OTHER)]
    assert hits[0]["chunk_id"] == earlier["chunk_id"]
    assert [s["hits"] for s in run["result"]["steps"]] == [1, 1, 1]
    # The answer cites both.
    assert run["sources"] == [
        {**hit, "node": "agent", "node_title": "检索资料并回答", "cited": True} for hit in hits
    ]
    flow = await p.service.flow(run["run_id"])
    output = next(s for s in flow["steps"] if s["node"] == "agent")["output"]
    assert output["citations"] == {h["chunk_id"]: h["index"] for h in hits}


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({}, "query 必须是非空字符串"),
        ({"query": ""}, "query 必须是非空字符串"),
        ({"query": "  "}, "query 必须是非空字符串"),
        ({"query": None}, "query 必须是非空字符串"),
        ({"query": 123}, "query 必须是非空字符串"),
        ({"query": ["退货"]}, "query 必须是非空字符串"),
        ({"query": POLICY, "knowledge_bases": "售后政策"}, "知识库名称列表"),
        ({"query": POLICY, "knowledge_bases": None}, "知识库名称列表"),
        ({"query": POLICY, "knowledge_bases": [{}]}, "知识库名称列表"),
        ({"query": POLICY, "knowledge_bases": ["未授权资料"]}, "没有知识库「未授权资料」"),
    ],
)
async def test_invalid_knowledge_calls_retry_json_without_spending_a_step(
    p, monkeypatch, args, reason
):
    kb_id = await p.seed()
    await p.knowledge.create("未授权资料")  # Exists, but is outside the agent's selection.
    config = agent_flow([kb_id])
    config["graph"]["nodes"][1]["max_steps"] = 1
    await p.publish(config)
    retrieval = AsyncMock(wraps=search)
    monkeypatch.setattr("aep.runtime.flow.nodes.search", retrieval)
    p.gateway.replies = [
        call("kb", **args),
        call("kb", query=POLICY, knowledge_bases=["售后政策"]),
        answer("三十天。[1]"),
    ]
    run = await p.run()
    assert run["status"] == "done", run["error"]
    assert run["result"]["answer"] == "三十天。[1]"
    assert len(run["result"]["steps"]) == 1
    retrieval.assert_awaited_once()
    assert len(p.gateway.requests) == 3
    assert reason in p.gateway.requests[1]["body"]["messages"][-1]["content"]
    assert all("已经达到步数上限" not in str(r["body"]) for r in p.gateway.requests)


@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid"])
async def test_agent_selects_bases_by_name_and_uses_configured_retrieval(p, monkeypatch, mode):
    first = await p.seed()
    second = await p.seed(OTHER, "建表规范")
    retrieval = {
        "mode": mode,
        "vector_weight": 0.4,
        "rerank": {"model": "mock-rerank", "candidates": 5},
    }
    await p.publish(agent_flow([first, second], top_k=1, min_score=0.8, retrieval=retrieval))
    searched = AsyncMock(wraps=search)
    monkeypatch.setattr("aep.runtime.flow.nodes.search", searched)
    p.gateway.replies = [
        call("kb", query=OTHER, knowledge_bases=["建表规范"]),
        answer("按建表规范回答。[1]"),
    ]
    run = await p.run()
    assert run["status"] == "done", run["error"]
    [hit] = run["result"]["hits"]
    assert hit["kb_id"] == second and hit["index"] == 1
    assert hit["scores"]["rerank"] == 1
    assert (hit["scores"]["vector"] is None) == (mode == "keyword")
    assert (hit["scores"]["keyword"] is None) == (mode == "vector")
    searched.assert_awaited_once_with(
        p.knowledge, p.gateway.client, [second], OTHER, 1, 0.8, retrieval, settings=p.settings
    )


@pytest.mark.parametrize("failure", ["embedding", "missing_base", "rerank"])
async def test_agent_reports_retrieval_errors_to_model_and_still_answers(p, monkeypatch, failure):
    kb_id = await p.seed()
    config = agent_flow([kb_id])
    if failure == "rerank":
        config["graph"]["nodes"][1]["knowledge"]["retrieval"] = {
            "mode": "keyword",
            "rerank": {"model": "mock-rerank"},
        }
    await p.publish(config)
    reason = {"embedding": "向量服务离线", "missing_base": kb_id, "rerank": "重排服务离线"}[failure]
    if failure == "missing_base":
        await p.knowledge.delete(kb_id)
    else:
        target = "aep.llm.embed" if failure == "embedding" else "aep.knowledge.search.rerank"
        monkeypatch.setattr(target, AsyncMock(side_effect=RuntimeError(reason)))
    p.gateway.replies = [call("kb", query=POLICY), answer("资料暂时查不到，请稍后重试。")]
    run = await p.run()
    assert run["status"] == "done", run["error"]
    assert run["result"]["answer"] == "资料暂时查不到，请稍后重试。"
    [step] = run["result"]["steps"]
    assert reason in step["error"] and step["hits"] == 0
    assert run["result"]["hits"] == [] and run["sources"] == []
    reply = p.gateway.requests[-1]["body"]["messages"][-1]["content"]
    assert "出错了" in reply and reason in reply


async def test_agent_reports_no_hits_to_model(p):
    kb_id = await p.seed()
    await p.publish(agent_flow([kb_id], min_score=1))
    p.gateway.replies = [call("kb", query="unrelated question"), answer("没有找到相关资料。")]
    run = await p.run()
    assert run["status"] == "done", run["error"]
    assert run["result"]["hits"] == [] and run["sources"] == []
    [step] = run["result"]["steps"]
    assert step["hits"] == 0 and step["error"] is None
    assert p.gateway.requests[-1]["body"]["messages"][-1]["content"].endswith("没有找到相关资料")


async def test_agent_truncates_model_context_and_single_node_tests_start_numbering_at_one(
    p, monkeypatch
):
    kb_id = await p.seed()
    [hit] = await search(p.knowledge, p.gateway.client, [kb_id], POLICY)
    hit = replace(hit, text="资料" * MAX_TOOL_RESULT)
    monkeypatch.setattr("aep.runtime.flow.nodes.search", AsyncMock(return_value=[hit]))
    await p.publish(agent_flow([kb_id]))
    for _ in range(2):
        p.gateway.replies = [call("kb", query=POLICY), answer("答案。[1]")]
        tried = await p.api.post("/apps/ask-policy/nodes/agent/test", json={})
        assert tried.status_code == 200, tried.text
        output = tried.json()
        assert output["status"] == "done"
        assert output["outputs"]["hits"] == [{**asdict(hit), "index": 1}]
        reply = p.gateway.requests[-1]["body"]["messages"][-1]["content"]
        assert (
            reply
            == "工具 kb 返回：\n" + context([IndexedHit(**asdict(hit), index=1)])[:MAX_TOOL_RESULT]
        )
    assert await p.service.recent() == []


@pytest.mark.parametrize("gives_answer", [False, True])
async def test_knowledge_search_counts_toward_max_steps(p, monkeypatch, gives_answer):
    kb_id = await p.seed()
    config = agent_flow([kb_id])
    config["graph"]["nodes"][1]["max_steps"] = 1
    await p.publish(config)
    searched = AsyncMock(wraps=search)
    monkeypatch.setattr("aep.runtime.flow.nodes.search", searched)
    p.gateway.replies = [
        call("kb", query=POLICY),
        call("kb", query=POLICY),
        answer("最终回答。[1]") if gives_answer else call("kb", query=POLICY),
    ]
    run = await p.run()
    searched.assert_awaited_once()
    assert "已经达到步数上限" in p.gateway.requests[-1]["body"]["messages"][-1]["content"]
    if gives_answer:
        assert run["status"] == "done", run["error"]
        assert len(run["result"]["steps"]) == 1
        assert run["result"]["steps"][0]["hits"] == 1
        assert run["sources"][0]["index"] == 1
    else:
        assert run["status"] == "error"
        assert "调用了 1 次工具仍没有给出回答" in run["error"]


@pytest.mark.parametrize("with_knowledge_node", [False, True])
async def test_agent_export_import_tracks_bases_once_and_reports_missing_bases(
    p, with_knowledge_node
):
    kb = await p.knowledge.create("政策")
    config = agent_flow([kb.kb_id])
    if with_knowledge_node:
        config["graph"]["nodes"].insert(1, knowledge_flow([kb.kb_id])["graph"]["nodes"][1])
        config["graph"]["edges"] = [
            {"from": "start", "to": "kb"},
            {"from": "kb", "to": "agent"},
            {"from": "agent", "to": "end"},
        ]
    await p.publish(config)
    exported = await p.api.get("/apps/ask-policy/versions/2/export")
    assert exported.status_code == 200
    data = yaml.safe_load(exported.text)
    assert data["requires"]["knowledge_bases"] == [{"kb_id": kb.kb_id, "name": "政策"}]
    imported = await p.api.post("/apps/import", json={"app": "copy-ok", "yaml": exported.text})
    assert imported.status_code == 201, imported.text
    assert imported.json()["problems"] == []
    assert imported.json()["app"]["draft"] is None
    await p.knowledge.delete(kb.kb_id)
    imported = await p.api.post("/apps/import", json={"app": "copy-missing", "yaml": exported.text})
    assert imported.status_code == 201, imported.text
    problems = [
        {
            "kind": "knowledge_base",
            "kb_id": kb.kb_id,
            "name": "政策",
            "message": "没有这个知识库，请在节点里重新选择",
        }
    ]
    assert imported.json()["problems"] == problems
    detail = imported.json()["app"]
    assert detail["current"]["config"] == builtin.blank()
    assert detail["draft"]["config"] == data["config"]
    saved = await p.api.put("/apps/copy-missing/draft", json={"config": data["config"]})
    assert saved.status_code == 422
    assert any(
        error["loc"][-4:] == ["agent", "knowledge", "knowledge_bases", 0]
        for error in saved.json()["detail"]
    )
    into = await p.api.post("/apps/copy-ok/draft/import", json={"yaml": exported.text})
    assert into.status_code == 200 and into.json()["problems"] == problems


async def test_current_agent_references_block_deletion_including_archived_apps(p):
    kb = await p.knowledge.create("政策")
    config = agent_flow([kb.kb_id])
    for name in ("first-app", "second-app"):
        await p.publish(config, name)
    assert (await p.api.delete("/apps/second-app")).status_code == 204
    deleted = await p.api.delete(f"/knowledge-bases/{kb.kb_id}")
    assert deleted.status_code == 409
    assert all(name in deleted.json()["detail"] for name in ("first-app", "second-app"))
    assert await p.knowledge.get(kb.kb_id) is not None
    for name in ("first-app", "second-app"):
        await p.service.versions.save_draft(name, builtin.blank(), None)
        await p.service.versions.publish(name, "移除知识库", None)
        await p.service.versions.save_draft(name, config, None)
    assert (await p.api.delete(f"/knowledge-bases/{kb.kb_id}")).status_code == 204
