"""Flow apps on real Postgres: the apps table, and a form waiting across a restart.

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
import uuid
from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.datasources import MemoryDataSourceStore
from aep.knowledge.store import Document, PostgresKnowledgeStore
from aep.knowledge.worker import KnowledgeWorker
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow.store import AppExists, PostgresFlowAppStore, PostgresSecretStore
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import PostgresRunStore
from aep.runtime.versions import PostgresAppVersionStore
from tests.conftest import SETTINGS, FakeGateway
from tests.test_flow_knowledge import POLICY, knowledge_flow

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@asynccontextmanager
async def platform(gateway: FakeGateway):
    """One 'process': its own pool, stores, service and catalog."""
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        store = PostgresRunStore(pool)
        await store.setup()
        versions = PostgresAppVersionStore(pool)
        await versions.setup()
        flows = PostgresFlowAppStore(pool)
        await flows.setup()
        knowledge = PostgresKnowledgeStore(pool)
        await knowledge.setup()
        service = RunService(
            store,
            checkpointer,
            default_apps(),
            AppContext(llm_client=gateway.client, settings=SETTINGS, knowledge=knowledge),
            versions=versions,
            flows=flows,
        )
        yield service, AppCatalog(service, MemoryDataSourceStore(), SETTINGS)
    finally:
        await pool.close()


async def test_flow_app_store():
    name = f"app-{uuid.uuid4().hex[:8]}"
    async with platform(FakeGateway()) as (service, _):
        flows = service.flows
        made = await flows.create(name, "客服", "分流", "张三")
        assert (made.title, made.created_by, made.archived_at) == ("客服", "张三", None)
        with pytest.raises(AppExists):
            await flows.create(name, "重名", "", None)
        await flows.update(name, None, "新的简介")
        assert (await flows.get(name)).summary == "新的简介"
        assert name in {a.app for a in await flows.all()}
        await flows.archive(name)
        assert name not in {a.app for a in await flows.all()}
        assert (await flows.get(name)).archived_at is not None
        # Shipping an app again does not bring an archived one back.
        await flows.ensure(name, "x", "y")
        assert (await flows.get(name)).archived_at is not None


async def test_a_form_waits_across_a_restart():
    name = f"orders-{uuid.uuid4().hex[:8]}"
    config = {
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [{"key": "q", "label": "问题", "type": "text"}],
                },
                {
                    "id": "ask",
                    "type": "human",
                    "title": "补充",
                    "mode": "form",
                    "prompt": "订单号？",
                    "fields": [
                        {"key": "order_id", "label": "订单号", "type": "text", "required": True}
                    ],
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "order", "from": ["{{ask.order_id}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
        }
    }
    gateway = FakeGateway()
    async with platform(gateway) as (service, catalog):
        await catalog.create(name, "订单", "", {"blank": True}, None)
        await catalog.save_draft(name, config, None)
        await catalog.publish(name, "表单", None)
        record = await service.create(name, {"q": "查订单"})
        await service.wait_idle()
        assert (await service.get(record.run_id)).pending["kind"] == "form"

    async with platform(gateway) as (restarted, catalog):
        assert (await catalog.detail(name))["current_version"] == 2
        await restarted.resume(record.run_id, "submit", None, "小王", {"order_id": "A1"})
        await restarted.wait_idle()
        view = await restarted.get(record.run_id)
        assert (view.record.status, view.result) == ("done", {"order": "A1"})


async def test_secret_store():
    app = f"app-{uuid.uuid4().hex[:8]}"
    async with platform(FakeGateway(["x"])) as (service, _):
        store = PostgresSecretStore(service.flows._pool)
        await store.setup()
        await store.setup()
        assert await store.get(app, "token") is None
        await store.set(app, "token", "one")
        await store.set(app, "token", "two")
        await store.set(app, "other", "x")
        assert await store.get(app, "token") == "two"
        assert [s.name for s in await store.names(app)] == ["other", "token"]
        await store.delete(app, "token")
        await store.delete(app, "token")
        assert [s.name for s in await store.names(app)] == ["other"]
        assert await store.names(f"{app}-none") == []


async def test_knowledge_node_citations_survive_a_postgres_restart(tmp_path):
    name = f"knowledge-{uuid.uuid4().hex[:8]}"
    gateway = FakeGateway()
    async with platform(gateway) as (service, catalog):
        base = await service.knowledge.create(name)
        path = tmp_path / "政策.md"
        path.write_text("# 退货期限\n\n" + POLICY)
        document = Document(
            str(uuid.uuid4()), base.kb_id, path.name, "md", path.stat().st_size, str(path)
        )
        await service.knowledge.add_documents(base.kb_id, [document])
        worker = KnowledgeWorker(service.knowledge, gateway.client, retry_delays=(0, 0))
        assert await worker.process_once()
        assert (
            await service.knowledge.get_document(base.kb_id, document.document_id)
        ).status == "ready"
        config = knowledge_flow([base.kb_id])
        kb2 = {**config["graph"]["nodes"][1], "id": "kb2", "title": "再次检索"}
        review = {
            "id": "review",
            "type": "human",
            "title": "继续",
            "prompt": "继续？",
            "options": [{"key": "approve", "label": "继续"}, {"key": "reject", "label": "重查"}],
        }
        config["graph"]["nodes"][2:2] = [review, kb2]
        config["graph"]["nodes"][-1]["outputs"] = [{"key": "hits", "from": ["{{kb2.hits}}"]}]
        config["graph"]["edges"] = [
            {"from": "start", "to": "kb"},
            {"from": "kb", "to": "review"},
            {"from": "review", "to": "kb2", "branch": "approve"},
            {"from": "review", "to": "kb", "branch": "reject"},
            {"from": "kb2", "to": "end"},
        ]
        await catalog.create(name, "知识问答", "", {"blank": True}, None)
        await catalog.save_draft(name, config, None)
        await catalog.publish(name, "选择知识库", None)
        record = await service.create(name, {"question": POLICY})
        await service.wait_idle()
        view = await service.get(record.run_id)
        assert view.record.status == "waiting", view.record.error
        [first] = view.sources
        assert first["index"] == 1
    async with platform(gateway) as (service, _):
        try:
            await service.resume(record.run_id, "approve", None)
            await service.wait_idle()
            view = await service.get(record.run_id)
            assert view.record.status == "done", view.record.error
            assert view.result["hits"][0]["index"] == 1
            assert view.sources == [{**first, "node": "kb2", "node_title": "再次检索"}]
            assert view.sources[0]["score"] == pytest.approx(1)
        finally:
            await service.knowledge.delete(base.kb_id)
            await gateway.client.close()
