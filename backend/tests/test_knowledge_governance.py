"""Knowledge governance (docs/api/knowledge-governance.md): owners, validity dates, document
versions, retrieval regression, hit counts and definition templates."""

from datetime import timedelta
from pathlib import Path

from aep.knowledge.search import search
from aep.knowledge.store import Document, ReplaceKind, today_in
from aep.runtime.flow.nodes import _whole
from tests.test_knowledge_api import POLICY, KnowledgeAPI

pytest_plugins = ("tests.test_knowledge_api",)

MEMBERS = "会员积分可以在下单时抵扣现金，一百积分抵扣一元，积分有效期为一年。"


async def ready_base(api: KnowledgeAPI) -> tuple[str, dict[str, dict]]:
    base = await api.create()
    uploaded = await api.upload(
        base["kb_id"], [("退货.txt", POLICY.encode()), ("会员.md", MEMBERS.encode())]
    )
    assert uploaded.status_code == 202, uploaded.text
    while await api.worker.process_once():
        pass
    docs = (await api.client.get(f"/knowledge-bases/{base['kb_id']}/documents")).json()
    return base["kb_id"], {doc["name"]: doc for doc in docs}


async def test_owner_and_validity_dates_filter_retrieval(api):
    kb, docs = await ready_base(api)
    doc = docs["退货.txt"]
    assert doc["owner"] is None and doc["valid_from"] is None and doc["valid_until"] is None
    assert doc["validity"] == "active" and doc["version"] == 1
    assert doc["hits_30d"] == 0 and doc["last_hit_at"] is None
    url = f"/knowledge-bases/{kb}/documents/{doc['document_id']}"

    today = today_in(api.settings.schedule_timezone)
    yesterday = (today - timedelta(days=1)).isoformat()
    bad = await api.client.patch(
        url, json={"valid_from": today.isoformat(), "valid_until": yesterday}
    )
    assert bad.status_code == 422
    assert bad.json()["detail"] == "失效日期不能早于生效日期"
    assert (await api.client.patch(url, json={"valid_from": "明天"})).status_code == 422

    changed = await api.client.patch(url, json={"owner": " 张三 ", "valid_until": yesterday})
    assert changed.status_code == 200, changed.text
    assert changed.json()["owner"] == "张三"
    assert changed.json()["validity"] == "expired"
    base = (await api.client.get(f"/knowledge-bases/{kb}")).json()
    assert base["validity_counts"] == {"active": 1, "pending": 0, "expired": 1}

    body = {"query": "退货", "min_score": 0, "retrieval": {"mode": "keyword"}}
    found = (await api.client.post(f"/knowledge-bases/{kb}/search", json=body)).json()
    assert "退货.txt" not in {hit["document_name"] for hit in found["hits"]}
    found = (
        await api.client.post(
            f"/knowledge-bases/{kb}/search", json={**body, "include_inactive": True}
        )
    ).json()
    assert "退货.txt" in {hit["document_name"] for hit in found["hits"]}

    # Not yet in effect is skipped too; clearing the dates brings it back.
    tomorrow = (today + timedelta(days=1)).isoformat()
    pending = await api.client.patch(url, json={"valid_from": tomorrow, "valid_until": None})
    assert pending.json()["validity"] == "pending"
    store = api.store
    hits = await search(store, api.gateway.client, [kb], "退货", 5, 0, {"mode": "keyword"})
    assert all(hit.document_name != "退货.txt" for hit in hits)
    cleared = await api.client.patch(url, json={"valid_from": None, "owner": ""})
    assert cleared.json()["validity"] == "active" and cleared.json()["owner"] is None
    hits = await search(store, api.gateway.client, [kb], "退货", 5, 0, {"mode": "keyword"})
    assert any(hit.document_name == "退货.txt" for hit in hits)


async def test_full_text_reading_skips_documents_not_in_effect(api):
    kb, docs = await ready_base(api)
    today = today_in(api.settings.schedule_timezone)
    await api.client.patch(
        f"/knowledge-bases/{kb}/documents/{docs['会员.md']['document_id']}",
        json={"valid_until": (today - timedelta(days=1)).isoformat()},
    )
    whole, _ = await _whole(api.store, [kb], 100_000, today)
    assert {hit.document_name for hit in whole} == {"退货.txt"}


async def test_replacing_a_document_keeps_its_id_and_adds_a_version(api):
    kb, docs = await ready_base(api)
    old = docs["退货.txt"]
    old_path = api.store._documents[old["document_id"]].path
    await api.client.patch(
        f"/knowledge-bases/{kb}/documents/{old['document_id']}", json={"owner": "张三"}
    )
    newer = "消费者收到商品后七天内可以无理由退货。"
    replaced = await api.client.post(
        f"/knowledge-bases/{kb}/documents",
        files=[("files", ("退货.txt", newer.encode()))],
        data={"replace": "true", "uploaded_by": "李四"},
    )
    assert replaced.status_code == 202, replaced.text
    [doc] = replaced.json()
    assert doc["document_id"] == old["document_id"]
    assert doc["version"] == 2 and doc["status"] == "queued"
    assert doc["owner"] == "张三" and doc["uploaded_by"] == "李四"
    assert not Path(old_path).exists()
    while await api.worker.process_once():
        pass
    file = await api.client.get(f"/knowledge-bases/{kb}/documents/{doc['document_id']}/file")
    assert file.text == newer
    versions = (
        await api.client.get(f"/knowledge-bases/{kb}/documents/{doc['document_id']}/versions")
    ).json()
    assert [(v["version"], v["uploaded_by"]) for v in versions] == [(2, "李四"), (1, None)]

    twice = await api.client.post(
        f"/knowledge-bases/{kb}/documents",
        files=[("files", ("会员.md", b"x")), ("files", ("会员.md", b"y"))],
        data={"replace": "true"},
    )
    assert twice.status_code == 422
    assert twice.json()["detail"] == "这次上传里有两个「会员.md」，替换时每个文件名只能传一个"
    # Without replace, the same name is still kept apart.
    copy = await api.upload(kb, [("退货.txt", b"x")])
    assert copy.json()[0]["name"] == "退货 (2).txt" and copy.json()[0]["version"] == 1


async def test_replacing_with_another_file_type_is_refused(api):
    base = await api.create()
    kb = base["kb_id"]
    await api.upload(kb, [("规则.md", b"# a")])
    # The type comes from the extension, so through the API the same name is the same type;
    # the store still refuses a mismatch.
    [doc] = await api.store.documents(kb)
    try:
        await api.store.add_documents(
            kb, [Document("x", kb, "规则.md", "txt", 1, "/unused")], replace=True
        )
    except ReplaceKind as exc:
        assert str(exc) == "「规则.md」原来是 md，替换时也要传 md"
    else:
        raise AssertionError("expected ReplaceKind")
    assert (await api.store.get_document(kb, doc.document_id)).version == 1


async def test_regression_cases_run_and_go_stale(api):
    kb, docs = await ready_base(api)
    url = f"/knowledge-bases/{kb}/regression"
    empty = (await api.client.get(url)).json()
    assert empty["cases"] == [] and empty["last_run"] is None and empty["stale"] is False
    assert empty["retrieval"]["mode"] == "hybrid"
    assert (await api.client.post(f"{url}/run")).status_code == 409

    cases = [
        {"id": "c1", "query": "退货", "expected": ["退货.txt"]},
        {"id": "c2", "query": "积分", "expected": ["会员.md", "退货.txt"]},
    ]
    keyword = {"mode": "keyword", "vector_weight": 0.7, "rerank": None}
    missing = await api.client.put(
        url, json={"cases": [{"id": "c1", "query": "q", "expected": ["没有.pdf"]}]}
    )
    assert missing.status_code == 422
    assert missing.json()["detail"] == "知识库里没有文档「没有.pdf」"
    dup = await api.client.put(url, json={"cases": [cases[0], cases[0]]})
    assert dup.json()["detail"] == "用例编号「c1」重复了"
    assert (
        await api.client.put(url, json={"cases": [{**cases[0], "id": "C 1"}]})
    ).status_code == 422

    saved = await api.client.put(
        url, json={"cases": cases, "retrieval": keyword, "min_score": 0, "actor": "王五"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["updated_by"] == "王五" and saved.json()["stale"] is True

    ran = (await api.client.post(f"{url}/run", json={"actor": "王五"})).json()
    run = ran["last_run"]
    assert ran["stale"] is False and run["by"] == "王五"
    assert (run["passed"], run["total"], run["score"]) == (1, 2, 0.5)
    first, second = run["results"]
    assert first["passed"] and first["found"] == ["退货.txt"] and first["error"] is None
    assert not second["passed"] and second["missing"] == ["退货.txt"]
    assert second["top"][0] == "会员.md"

    # Changing a document's dates makes it stale; the expired document is then missing.
    today = today_in(api.settings.schedule_timezone)
    await api.client.patch(
        f"/knowledge-bases/{kb}/documents/{docs['退货.txt']['document_id']}",
        json={"valid_until": (today - timedelta(days=1)).isoformat()},
    )
    assert (await api.client.get(url)).json()["stale"] is True
    ran = (await api.client.post(f"{url}/run")).json()
    assert ran["last_run"]["passed"] == 0 and ran["last_run"]["by"] is None
    # Regression runs do not count as use by a run.
    docs_now = (await api.client.get(f"/knowledge-bases/{kb}/documents")).json()
    assert all(doc["hits_30d"] == 0 for doc in docs_now)


async def test_runs_count_hits_once_per_document(api):
    kb, docs = await ready_base(api)
    for _ in range(2):
        await search(
            api.store, api.gateway.client, [kb], "退货", 5, 0, {"mode": "keyword"}, run_id="r1"
        )
    await search(
        api.store, api.gateway.client, [kb], "退货", 5, 0, {"mode": "keyword"}, run_id="r2"
    )
    await api.client.post(
        f"/knowledge-bases/{kb}/search",
        json={"query": "退货", "min_score": 0, "retrieval": {"mode": "keyword"}},
    )
    listed = {
        doc["name"]: doc
        for doc in (await api.client.get(f"/knowledge-bases/{kb}/documents")).json()
    }
    assert listed["退货.txt"]["hits_30d"] == 2
    assert listed["退货.txt"]["last_hit_at"] is not None
    assert listed["会员.md"]["hits_30d"] == 0
    one = await api.client.get(f"/knowledge-bases/{kb}/documents/{docs['退货.txt']['document_id']}")
    assert one.json()["hits_30d"] == 2


async def test_doc_templates(api):
    templates = (await api.client.get("/knowledge-bases/doc-templates")).json()
    assert [t["id"] for t in templates] == ["metric", "table", "rule"]
    metric = templates[0]
    assert metric["title"] == "指标口径" and metric["filename"] == "指标口径-{名称}.md"
    for section in ("定义", "计算公式", "统计粒度", "时间口径", "包含和排除", "数据来源"):
        assert f"## {section}" in metric["body"]
    assert all("{名称}" in t["body"] for t in templates)


def test_catalog_quality_comes_from_the_last_regression():
    from datetime import UTC, datetime

    from aep.knowledge.governance import quality
    from aep.knowledge.store import KnowledgeBase, Regression

    ran = datetime(2026, 11, 1, tzinfo=UTC)
    base = KnowledgeBase("kb", "售后")
    case = {"id": "c1", "query": "q", "expected": ["a.md"]}
    assert quality(None, base) is None
    assert quality(Regression("kb", [case]), base) is None
    run = {"at": ran.isoformat(), "passed": 9, "total": 10, "score": 0.9}
    record = Regression("kb", [case], updated_at=ran - timedelta(hours=1), last_run=run)
    assert quality(record, base) == (0.9, "10 条用例，通过 9 条")
    changed = KnowledgeBase("kb", "售后", docs_changed_at=ran + timedelta(minutes=1))
    assert quality(record, changed) == (0.9, "10 条用例，通过 9 条，文档改过还没重跑")
