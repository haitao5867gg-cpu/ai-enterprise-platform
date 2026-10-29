"""HTTP, saved-node and ETL wiring for the retrieval contract."""

from unittest.mock import AsyncMock

import pytest

from aep.knowledge.rerank import RerankError
from aep.runtime import appconfig
from aep.runtime.apps import etl
from tests.test_etl import make_service, plan, reference_material, reply
from tests.test_flow_knowledge import knowledge_flow

pytest_plugins = ("tests.test_knowledge_api", "tests.test_flow_knowledge")

INVALID = [
    ({"mode": "other"}, ["mode"]),
    ({"mode": None}, ["mode"]),
    ({"vector_weight": -0.1}, ["vector_weight"]),
    ({"vector_weight": 1.1}, ["vector_weight"]),
    ({"vector_weight": True}, ["vector_weight"]),
    ({"vector_weight": "0.7"}, ["vector_weight"]),
    ({"vector_weight": None}, ["vector_weight"]),
    ({"rerank": "mock-rerank"}, ["rerank"]),
    ({"rerank": {}}, ["rerank", "model"]),
    ({"rerank": {"model": "missing"}}, ["rerank", "model"]),
    ({"rerank": {"model": "mock-embed"}}, ["rerank", "model"]),
    ({"rerank": {"model": "mock-rerank", "candidates": 4}}, ["rerank", "candidates"]),
    ({"rerank": {"model": "mock-rerank", "candidates": 51}}, ["rerank", "candidates"]),
    ({"rerank": {"model": "mock-rerank", "candidates": 5.5}}, ["rerank", "candidates"]),
    ({"rerank": {"model": "mock-rerank", "candidates": True}}, ["rerank", "candidates"]),
    ({"rerank": {"model": "mock-rerank", "candidates": None}}, ["rerank", "candidates"]),
]


@pytest.mark.parametrize(("retrieval", "loc"), INVALID)
async def test_search_rejects_invalid_retrieval_with_422(api, retrieval, loc):
    kb_id = (await api.create())["kb_id"]
    response = await api.client.post(
        f"/knowledge-bases/{kb_id}/search", json={"query": "问题", "retrieval": retrieval}
    )
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert api.gateway.requests == []


@pytest.mark.parametrize(("retrieval", "loc"), INVALID)
async def test_saved_nodes_report_exact_retrieval_location(p, retrieval, loc):
    kb = await p.knowledge.create("资料")
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    response = await p.api.put(
        "/apps/ask-policy/draft", json={"config": knowledge_flow([kb.kb_id], retrieval=retrieval)}
    )
    assert response.status_code == 422, response.text
    assert ["body", "config", "graph", "nodes", "kb", "retrieval", *loc] in [
        error["loc"] for error in response.json()["detail"]
    ]


async def test_candidates_cannot_be_less_than_top_k_in_search_or_node(p):
    kb = await p.knowledge.create("资料")
    retrieval = {"rerank": {"model": "mock-rerank", "candidates": 5}}
    response = await p.api.post(
        f"/knowledge-bases/{kb.kb_id}/search",
        json={"query": "问题", "top_k": 6, "retrieval": retrieval},
    )
    assert response.status_code == 422 and "不能少于" in response.json()["detail"]
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    response = await p.api.put(
        "/apps/ask-policy/draft",
        json={"config": knowledge_flow([kb.kb_id], top_k=6, retrieval=retrieval)},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-3:] == ["retrieval", "rerank", "candidates"]


async def test_options_share_app_chat_models_and_hide_embedding_and_rerank_aliases(p):
    p.settings.rerank_models = ["mock-rerank", "company-rerank"]
    p.catalog._models = AsyncMock(return_value=["mock", "company", "company-embed", "other-rerank"])
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    options = (await p.api.get("/knowledge-bases/options")).json()
    detail = (await p.api.get("/apps/ask-policy")).json()
    assert options["retrieval"] == {"mode": "hybrid", "vector_weight": 0.7, "rerank": None}
    assert (
        options["rerank_models"]
        == detail["options"]["rerank_models"]
        == [
            {"model": "mock-rerank", "kind": "rerank"},
            {"model": "company-rerank", "kind": "rerank"},
            {"model": "mock", "kind": "llm"},
            {"model": "company", "kind": "llm"},
        ]
    )


async def test_options_do_not_invent_chat_models_when_gateway_returns_empty_list(p):
    p.catalog._models = AsyncMock(return_value=[])
    await p.api.post("/apps", json={"app": "ask-policy", "title": "问答"})
    options = (await p.api.get("/knowledge-bases/options")).json()
    detail = (await p.api.get("/apps/ask-policy")).json()
    assert detail["options"]["models"] == []
    assert options["rerank_models"] == detail["options"]["rerank_models"]
    assert options["rerank_models"] == [{"model": "mock-rerank", "kind": "rerank"}]


async def test_keyword_api_and_node_pass_retrieval_and_return_scores_without_embedding(
    p, monkeypatch
):
    kb_id = await p.seed()
    broken_embedding = AsyncMock(side_effect=RuntimeError("向量服务离线"))
    monkeypatch.setattr("aep.llm.embed", broken_embedding)
    retrieval = {"mode": "keyword", "vector_weight": 0.7, "rerank": None}
    response = await p.api.post(
        f"/knowledge-bases/{kb_id}/search", json={"query": "退货期限", "retrieval": retrieval}
    )
    assert response.status_code == 200, response.text
    [hit] = response.json()["hits"]
    assert hit["scores"] == {"vector": None, "keyword": 1, "rerank": None}
    await p.publish(knowledge_flow([kb_id], retrieval=retrieval, query="退货期限"))
    run = await p.run()
    assert run["status"] == "done", run["error"]
    assert run["result"]["hits"][0]["scores"] == hit["scores"]
    assert run["sources"][0]["scores"] == hit["scores"]
    tried = (await p.api.post("/apps/ask-policy/nodes/kb/test", json={})).json()
    assert tried["outputs"]["hits"][0]["scores"] == hit["scores"]
    broken_embedding.assert_not_awaited()


async def test_llm_rerank_bad_format_returns_502_reason_and_node_knowledge_error(p):
    kb_id = await p.seed()
    p.gateway.replies = ["not JSON"]
    retrieval = {"mode": "keyword", "rerank": {"model": "mock"}}
    response = await p.api.post(
        f"/knowledge-bases/{kb_id}/search", json={"query": "退货期限", "retrieval": retrieval}
    )
    assert response.status_code == 502
    assert "输出格式不正确" in response.json()["detail"]
    assert len(p.gateway.requests) == 2
    await p.publish(
        knowledge_flow(
            [kb_id],
            query="退货期限",
            retrieval=retrieval,
            on_error={"then": "default", "default": {"hits": [], "context": "检索失败"}},
        )
    )
    tried = (await p.api.post("/apps/ask-policy/nodes/kb/test", json={})).json()
    assert tried["status"] == "error" and tried["error"]["type"] == "knowledge_error"
    assert "输出格式不正确" in tried["error"]["message"]
    run = await p.run()
    assert run["status"] == "done", run["error"]
    assert run["result"] == {"hits": [], "context": "检索失败"}
    assert run["sources"] == []


async def test_dedicated_rerank_failure_returns_502_reason(p, monkeypatch):
    kb_id = await p.seed()
    p.settings.rerank_models = ["company-rerank"]
    failing = AsyncMock(side_effect=RerankError("重排服务超时"))
    monkeypatch.setattr("aep.knowledge.search.rerank", failing)
    response = await p.api.post(
        f"/knowledge-bases/{kb_id}/search",
        json={
            "query": "退货期限",
            "retrieval": {"mode": "keyword", "rerank": {"model": "company-rerank"}},
        },
    )
    assert response.status_code == 502 and response.json()["detail"] == "重排服务超时"
    assert failing.call_args.args[-1] == "rerank"


@pytest.mark.parametrize("mode", ["hybrid", "vector", "keyword", None])
async def test_etl_plan_uses_configured_mode_and_hybrid_default(mode, monkeypatch):
    service, gateway, ds = await make_service([reply(plan())])
    try:
        config = await reference_material(service, gateway)
        if mode is None:
            config["nodes"]["plan"]["params"].pop("knowledge_mode")
        else:
            config["nodes"]["plan"]["params"]["knowledge_mode"] = mode
        retrieval = AsyncMock(return_value=[])
        monkeypatch.setattr("aep.runtime.apps.etl.search", retrieval)
        record = await service.create(
            "etl", {"datasource_id": ds, "request": "做一张订单明细宽表"}, config=config
        )
        await service.wait_idle()
        view = await service.get(record.run_id)
        assert view.record.status == "waiting", view.record.error
        retrieval.assert_awaited_once()
        assert retrieval.call_args.kwargs["retrieval"] == {
            "mode": mode or "hybrid",
            "vector_weight": 0.7,
            "rerank": None,
        }
    finally:
        await gateway.client.close()


async def test_etl_mode_choices_and_validation(p):
    for mode in ["invalid", None, 1]:
        response = await p.api.put(
            "/apps/etl/draft",
            json={"config": {"nodes": {"plan": {"params": {"knowledge_mode": mode}}}}},
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][-1] == "knowledge_mode"
    assert appconfig.builtin(etl.CONFIG["plan"]).params["knowledge_mode"] == "hybrid"
    setting = next(p.setting() for p in etl.CONFIG["plan"].params if p.key == "knowledge_mode")
    assert setting == {
        "key": "knowledge_mode",
        "label": "检索方式",
        "type": "enum",
        "choices": [
            {"value": "hybrid", "label": "混合"},
            {"value": "vector", "label": "向量"},
            {"value": "keyword", "label": "关键词"},
        ],
    }
