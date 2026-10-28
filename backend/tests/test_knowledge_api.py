"""Knowledge endpoints against in-memory storage and the in-process model gateway."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

import httpx
import pytest

from aep.config import Settings, get_settings
from aep.knowledge.search import search
from aep.knowledge.store import Document, MemoryKnowledgeStore
from aep.knowledge.worker import KnowledgeWorker
from aep.main import create_app
from tests.conftest import FakeGateway
from tests.test_knowledge_extract import write_pdf

POLICY = "消费者收到商品后三十天内可以申请退货，商品需要保持原有包装并附带购买凭证。"


@dataclass
class KnowledgeAPI:
    client: httpx.AsyncClient
    store: MemoryKnowledgeStore
    worker: KnowledgeWorker
    settings: Settings
    gateway: FakeGateway

    async def create(self, **values: Any) -> dict:
        response = await self.client.post("/knowledge-bases", json={"name": "售后政策", **values})
        assert response.status_code == 201, response.text
        return response.json()

    async def upload(self, kb: str, files: list[tuple[str, bytes]] | None = None) -> httpx.Response:
        return await self.client.post(
            f"/knowledge-bases/{kb}/documents",
            files=[
                ("files", (name, content))
                for name, content in (files or [("退货.txt", POLICY.encode())])
            ],
        )


@pytest.fixture
async def api(tmp_path, gateway):
    app = create_app()
    settings = Settings(knowledge_dir=tmp_path / "knowledge")
    store = MemoryKnowledgeStore()
    worker = KnowledgeWorker(store, gateway.client, retry_delays=(0, 0))
    app.state.knowledge = store
    app.state.knowledge_worker = worker
    app.state.llm_client = gateway.client
    app.dependency_overrides[get_settings] = lambda: settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield KnowledgeAPI(client, store, worker, settings, gateway)
    await gateway.client.close()


async def test_options_and_base_crud(api):
    options = (await api.client.get("/knowledge-bases/options")).json()
    assert options == {
        "embedding_models": ["mock-embed", "company-embed"],
        "default_embedding_model": "mock-embed",
        "chunking": {"size": 800, "overlap": 100},
    }
    base = await api.create(name="B", description="说明")
    other = await api.create(name="A")
    assert base["document_count"] == base["chunk_count"] == 0
    assert base["status_counts"] == dict.fromkeys(["queued", "processing", "ready", "error"], 0)
    assert base["embedding_model"] == "mock-embed"
    url = f"/knowledge-bases/{base['kb_id']}"
    assert (await api.client.get(url)).json() == base
    assert [b["name"] for b in (await api.client.get("/knowledge-bases")).json()] == ["A", "B"]
    response = await api.client.patch(
        url,
        json={
            "name": "新名称",
            "description": "新描述",
            "embedding_model": "company-embed",
            "chunking": {"size": 200, "overlap": 30},
        },
    )
    assert response.status_code == 200
    assert response.json()["chunking"] == {"size": 200, "overlap": 30}
    assert response.json()["embedding_model"] == "company-embed"
    assert (await api.client.patch(url, json={"name": "A"})).status_code == 409
    assert (await api.client.post("/knowledge-bases", json={"name": "A"})).status_code == 409
    assert (await api.client.delete(url)).status_code == 204
    assert (await api.client.get(url)).status_code == 404
    assert (await api.client.get("/knowledge-bases")).json() == [other]


@pytest.mark.parametrize(
    "values",
    [
        {"name": " "},
        {"name": "字" * 51},
        {"description": "字" * 501},
        {"embedding_model": "unknown"},
        {"embedding_model": ""},
        {"chunking": {"size": 199}},
        {"chunking": {"size": 2001}},
        {"chunking": {"overlap": -1}},
        {"chunking": {"size": 200, "overlap": 101}},
        {"description": None},
    ],
)
async def test_invalid_base_fields_have_chinese_detail(api, values):
    response = await api.client.post("/knowledge-bases", json={"name": "资料", **values})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


async def test_options_and_creation_use_configured_default_model(api):
    api.settings.embedding_models = ["company-embed"]
    api.settings.default_embedding_model = "company-embed"
    assert (await api.create())["embedding_model"] == "company-embed"
    assert (await api.client.get("/knowledge-bases/options")).json()["embedding_models"] == [
        "company-embed"
    ]


async def test_invalid_json_uses_the_contract_error_format(api):
    response = await api.client.post(
        "/knowledge-bases", content=b"{", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert "参数" in response.json()["detail"]


async def test_upload_names_original_download_processing_and_cascading_deletion(api):
    base = await api.create()
    kb = base["kb_id"]
    response = await api.upload(kb, [("退货.TXT", POLICY.encode()), ("退货.TXT", POLICY.encode())])
    assert response.status_code == 202, response.text
    first, second = response.json()
    third = (await api.upload(kb, [("退货.TXT", POLICY.encode())])).json()[0]
    assert [d["name"] for d in [first, second, third]] == [
        "退货.TXT",
        "退货 (2).TXT",
        "退货 (3).TXT",
    ]
    assert all(d["status"] == "queued" and d["kind"] == "txt" for d in response.json())
    assert api.worker.wakeup.is_set()
    document_url = f"/knowledge-bases/{kb}/documents/{first['document_id']}"
    original = await api.client.get(document_url + "/file")
    assert original.content == POLICY.encode()
    assert (
        unquote(original.headers["content-disposition"]) == "attachment; filename*=UTF-8''退货.TXT"
    )
    files = list((api.settings.knowledge_dir / kb).rglob("*.TXT"))
    assert len(files) == 3
    assert (await api.client.get(f"/knowledge-bases/{kb}/documents")).json()[0] == third
    assert await api.worker.process_once()
    ready = (await api.client.get(document_url)).json()
    assert ready["status"] == "ready" and ready["chunk_count"] == 1
    assert "path" not in ready and "tenant_id" not in ready
    chunks = (await api.client.get(document_url + "/chunks")).json()
    assert chunks["total"] == 1 and chunks["items"][0]["text"] == POLICY
    assert "embedding" not in chunks["items"][0]
    counts = (await api.client.get(f"/knowledge-bases/{kb}")).json()
    assert counts["status_counts"]["ready"] == 1 and counts["status_counts"]["queued"] == 2
    assert counts["chunk_count"] == 1
    assert (await api.client.delete(document_url)).status_code == 204
    assert (await api.client.get(document_url)).status_code == 404
    assert await api.store.chunks(first["document_id"], 0, 50) == (0, [])
    assert not (api.settings.knowledge_dir / kb / first["document_id"]).exists()
    assert (await api.client.delete(f"/knowledge-bases/{kb}")).status_code == 204
    assert not (api.settings.knowledge_dir / kb).exists()
    assert await api.store.documents(kb) == []


@pytest.mark.parametrize("kind", ["extension", "size", "count", "capacity"])
async def test_upload_limits_reject_the_entire_batch_without_files(api, kind):
    kb = (await api.create())["kb_id"]
    files = [("ok.txt", POLICY.encode())]
    if kind == "extension":
        files.append(("bad.exe", b"bad"))
        detail = "bad.exe"
    elif kind == "size":
        files.append(("big.pdf", b"x" * (20 * 1024 * 1024 + 1)))
        detail = "big.pdf"
    elif kind == "count":
        files *= 21
        detail = "20"
    else:
        await api.store.add_documents(
            kb, [Document(str(uuid4()), kb, f"{i}.txt", "txt", 0, "unused") for i in range(500)]
        )
        detail = "500"
    response = await api.upload(kb, files)
    assert response.status_code == 422, response.text
    assert detail in response.json()["detail"]
    assert not api.settings.knowledge_dir.exists()
    assert len(await api.store.documents(kb)) == (500 if kind == "capacity" else 0)


async def test_missing_upload_and_document_ownership(api):
    kb = (await api.create())["kb_id"]
    other = (await api.create(name="另一个库"))["kb_id"]
    doc = (await api.upload(kb)).json()[0]
    assert (await api.client.post(f"/knowledge-bases/{kb}/documents")).status_code == 422
    for suffix in ["", "/file", "/chunks"]:
        response = await api.client.get(
            f"/knowledge-bases/{other}/documents/{doc['document_id']}" + suffix
        )
        assert response.status_code == 404
    assert (await api.client.get("/knowledge-bases/not-a-uuid")).status_code == 404


async def test_model_edit_conflicts_with_documents_and_chunking_applies_on_reindex(api):
    kb = (await api.create())["kb_id"]
    document = (await api.upload(kb, [("长文.md", ("# 政策\n" + POLICY * 20).encode())])).json()[0]
    base_url = f"/knowledge-bases/{kb}"
    assert (
        await api.client.patch(base_url, json={"embedding_model": "company-embed"})
    ).status_code == 409
    assert (
        await api.client.patch(base_url, json={"embedding_model": "mock-embed"})
    ).status_code == 200
    await api.worker.process_once()
    doc_url = base_url + f"/documents/{document['document_id']}"
    before = (await api.client.get(doc_url + "/chunks")).json()
    assert all(c["heading"] == "政策" for c in before["items"])
    assert (
        await api.client.patch(base_url, json={"chunking": {"size": 200, "overlap": 20}})
    ).status_code == 200
    assert (await api.client.get(doc_url + "/chunks")).json() == before
    response = await api.client.post(doc_url + "/reindex")
    assert response.status_code == 202 and response.json()["status"] == "queued"
    assert (await api.client.get(doc_url + "/chunks")).json() == {"total": 0, "items": []}
    await api.worker.process_once()
    after = (await api.client.get(doc_url + "/chunks")).json()
    assert after["total"] > before["total"]
    page = (await api.client.get(doc_url + "/chunks?offset=1&limit=2")).json()
    assert page["total"] == after["total"]
    assert [c["position"] for c in page["items"]] == [1, 2]
    assert (await api.client.get(doc_url + "/chunks?offset=1000")).json()["items"] == []
    assert (await api.client.get(doc_url + "/chunks?limit=201")).status_code == 422
    assert (await api.client.get(doc_url + "/chunks?offset=-1")).status_code == 422
    response = await api.client.post(base_url + "/reindex")
    assert response.status_code == 202 and response.json() == {"queued": 1}


async def test_queued_documents_keep_upload_chunking_until_reindexed(api):
    kb = (await api.create())["kb_id"]
    base_url = f"/knowledge-bases/{kb}"
    first = (await api.upload(kb, [("before.txt", ("字" * 1000).encode())])).json()[0]
    # An omitted nested overlap takes the contract default, rather than crashing.
    response = await api.client.patch(base_url, json={"chunking": {"size": 200}})
    assert response.status_code == 200 and response.json()["chunking"] == {
        "size": 200,
        "overlap": 100,
    }
    second = (await api.upload(kb, [("after.txt", ("字" * 1000).encode())])).json()[0]
    await api.worker.process_once()
    await api.worker.process_once()
    old = await api.store.get_document(kb, first["document_id"])
    new = await api.store.get_document(kb, second["document_id"])
    assert old.chunk_count == 2 and new.chunk_count == 9
    await api.client.post(base_url + f"/documents/{first['document_id']}/reindex")
    await api.worker.process_once()
    assert (await api.store.get_document(kb, first["document_id"])).chunk_count == 9


async def test_gateway_failure_cleans_all_chunks_and_reindex_retries_successfully(api):
    kb = (await api.create(embedding_model="company-embed", chunking={"size": 200, "overlap": 0}))[
        "kb_id"
    ]
    document = (await api.upload(kb, [("long.txt", ("字" * 2400).encode())])).json()[0]
    api.gateway.embedding_statuses = [200, 500, 500, 500]
    await api.worker.process_once()
    doc_url = f"/knowledge-bases/{kb}/documents/{document['document_id']}"
    failed = (await api.client.get(doc_url)).json()
    assert failed["status"] == "error" and "provider key missing" in failed["error"]
    assert failed["chunk_count"] == 0
    assert (await api.client.get(doc_url + "/chunks")).json() == {"total": 0, "items": []}
    assert len(api.gateway.requests) == 4
    assert [len(r["body"]["input"]) for r in api.gateway.requests] == [10, 2, 2, 2]
    assert (await api.client.post(doc_url + "/reindex")).status_code == 202
    await api.worker.process_once()
    assert (await api.client.get(doc_url)).json()["status"] == "ready"


async def test_scanned_pdf_and_corrupt_document_fail_without_stopping_next_document(api, tmp_path):
    pdf = tmp_path / "scan.pdf"
    write_pdf(pdf, [None])
    kb = (await api.create())["kb_id"]
    docs = (
        await api.upload(
            kb,
            [
                ("扫描件.pdf", pdf.read_bytes()),
                ("损坏.docx", b"broken"),
                ("正常.txt", POLICY.encode()),
            ],
        )
    ).json()
    for _ in docs:
        assert await api.worker.process_once()
    statuses = [await api.store.get_document(kb, d["document_id"]) for d in docs]
    assert [d.status for d in statuses] == ["error", "error", "ready"]
    assert "没有可提取的文字" in statuses[0].error
    assert not await api.worker.process_once()


async def test_search_top_k_min_score_and_only_ready_documents(api):
    kb = (await api.create())["kb_id"]
    docs = (
        await api.upload(
            kb,
            [
                ("policy.txt", POLICY.encode()),
                ("long.txt", (POLICY * 60).encode()),
                ("queued.txt", POLICY.encode()),
            ],
        )
    ).json()
    await api.worker.process_once()
    await api.worker.process_once()
    url = f"/knowledge-bases/{kb}/search"
    response = await api.client.post(url, json={"query": "退货期限", "top_k": 2, "min_score": 0})
    assert response.status_code == 200, response.text
    hits = response.json()["hits"]
    assert len(hits) == 2 and response.json()["duration_ms"] >= 0
    assert all(h["document_id"] != docs[2]["document_id"] for h in hits)
    assert all(h["kb_id"] == kb and h["kb_name"] == "售后政策" for h in hits)
    assert hits[0]["score"] >= hits[1]["score"] >= 0
    assert (await api.client.post(url, json={"query": "数据库连接", "min_score": 1})).json()[
        "hits"
    ] == []
    # Mark a formerly ready document as processing, then error: neither is searchable.
    await api.store.requeue(kb, docs[0]["document_id"])
    claim = await api.store.claim()
    assert claim.document_id == docs[0]["document_id"]
    await api.store.fail(claim, "测试失败")
    remaining = (
        await api.client.post(url, json={"query": "退货", "min_score": 0, "top_k": 20})
    ).json()["hits"]
    assert all(h["document_id"] == docs[1]["document_id"] for h in remaining)


@pytest.mark.parametrize(
    "values",
    [
        {"query": ""},
        {"query": " "},
        {"query": "字" * 2001},
        {"top_k": 0},
        {"top_k": 21},
        {"min_score": -0.1},
        {"min_score": 1.1},
    ],
)
async def test_search_input_limits(api, values):
    kb = (await api.create())["kb_id"]
    response = await api.client.post(
        f"/knowledge-bases/{kb}/search", json={"query": "退货", **values}
    )
    assert response.status_code == 422


async def test_multiple_bases_merge_global_top_k_and_use_each_model(api):
    first = (await api.create())["kb_id"]
    second = (await api.create(name="第二个库", embedding_model="company-embed"))["kb_id"]
    await api.upload(first)
    await api.upload(second)
    await api.worker.process_once()
    await api.worker.process_once()
    hits = await search(
        api.store, api.gateway.client, [first, second], POLICY, top_k=1, min_score=0
    )
    assert len(hits) == 1 and hits[0].score == pytest.approx(1)
    all_hits = await search(
        api.store, api.gateway.client, [first, second], POLICY, top_k=2, min_score=0
    )
    assert {h.kb_id for h in all_hits} == {first, second}
