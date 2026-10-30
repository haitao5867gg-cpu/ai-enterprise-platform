"""Skills: the built-in ones, drafts and versions, and the Skills API."""

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.datasources import MemoryDataSourceStore
from aep.etl import codegen
from aep.etl.plan import OUTPUT_FORMAT
from aep.main import app
from aep.runtime.appconfig import builtin
from aep.runtime.apps import etl
from aep.runtime.catalog import AppCatalog
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.skills import (
    MemorySkillStore,
    NoDraft,
    SkillContent,
    SkillError,
    SkillExists,
    builtin_skills,
)
from tests.conftest import SETTINGS, FakeGateway

PLAN_RULES = "你是资深数据工程师"


def content(body: str = "先读需求，再看数据。", **changes) -> SkillContent:
    return SkillContent(changes.pop("description", "看数据的做法"), body, **changes)


def test_builtin_skills_are_version_one():
    skills = builtin_skills()
    assert set(skills) == {"etl-plan", "etl-codegen", "etl-plan-brief"}
    for skill in skills.values():
        assert skill.version == 1
        assert skill.note == "平台内置"
        assert skill.description
    assert skills["etl-plan"].body.startswith(PLAN_RULES)
    # How to write a version 2 plan (docs/api/data-nodes.md, 内置 Skill etl-plan-brief).
    brief = skills["etl-plan-brief"].body
    assert brief.startswith(PLAN_RULES)
    for text in ("brief", "rules", "basis", "questions", "count(order_id)", "2026-10"):
        assert text in brief


async def test_builtins_are_registered_only_under_new_names():
    store = MemorySkillStore(builtins=False)
    await store.create("etl-plan", content("我们公司的方案规范"))
    await store.publish("etl-plan", "公司规范", None)
    assert await store.register_builtins() == 2
    assert (await store.version("etl-plan", 1)).body == "我们公司的方案规范"
    assert (await store.version("etl-plan-brief", 1)).note == "平台内置"
    assert await store.register_builtins() == 0


def test_a_model_node_prompt_is_its_skills_then_the_output_format():
    plan = builtin(etl.CONFIG["plan"])
    assert [s.name for s in plan.skills] == ["etl-plan"]
    assert plan.prompt().startswith(PLAN_RULES)
    assert plan.prompt().endswith(OUTPUT_FORMAT)
    assert builtin(etl.CONFIG["codegen"]).prompt().endswith(codegen.OUTPUT_FORMAT)


def test_skill_text_includes_its_references():
    skill = content(references={"glossary.md": "GMV：成交总额"})
    assert skill.text() == "先读需求，再看数据。\n\n参考资料 glossary.md：\nGMV：成交总额"


async def test_drafts_become_versions_and_versions_never_change():
    store = MemorySkillStore()
    created = await store.create("sales-terms", content())
    assert created.published_version is None
    assert created.draft is not None

    v1 = await store.publish("sales-terms", "第一版", "张三")
    assert (v1.published_version, v1.draft) == (1, None)
    assert v1.current.created_by == "张三"

    await store.save_draft("sales-terms", content("改过的正文"))
    unchanged = await store.version("sales-terms", 1)
    assert unchanged.body == "先读需求，再看数据。"
    v2 = await store.publish("sales-terms", "第二版", None)
    assert [v.version for v in v2.versions] == [2, 1]
    assert (await store.version("sales-terms", 2)).body == "改过的正文"

    # A built-in skill is edited the same way; its version 1 stays.
    await store.save_draft("etl-plan", content("我们公司的方案规范"))
    assert (await store.publish("etl-plan", "公司规范", None)).published_version == 2
    assert (await store.version("etl-plan", 1)).body.startswith(PLAN_RULES)


async def test_store_refuses_bad_skills():
    store = MemorySkillStore()
    with pytest.raises(SkillExists):
        await store.create("etl-plan", content())
    with pytest.raises(SkillError):
        await store.create("Sales_Terms", content())
    with pytest.raises(SkillError) as raised:
        await store.create("ok-name", content("", description=""))
    assert [field for field, _ in raised.value.problems] == ["description", "body"]
    with pytest.raises(SkillError):
        await store.create("ok-name", content("x" * 20001))
    with pytest.raises(SkillError):
        await store.create("ok-name", content(references={"../secret.md": "x"}))
    with pytest.raises(SkillError):
        await store.create("ok-name", content(references={f"r{i}.md": "x" for i in range(11)}))
    with pytest.raises(NoDraft):
        await store.publish("etl-plan", "没有草稿", None)


@pytest.fixture
async def api():
    gateway = FakeGateway()
    service = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(llm_client=gateway.client, settings=SETTINGS),
    )
    app.state.run_service = service
    app.state.skills = service.skills
    app.state.catalog = AppCatalog(service, MemoryDataSourceStore(), SETTINGS)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_skills_api(api):
    listed = (await api.get("/skills")).json()
    assert [s["name"] for s in listed] == ["etl-codegen", "etl-plan", "etl-plan-brief"]
    plan = listed[1]
    assert plan["published_version"] == 1
    assert plan["has_draft"] is False
    assert plan["used_by"] == [{"app": "etl", "node": "plan", "version": 1, "skill_version": 1}]

    body = {"name": "sales-terms", "description": "销售术语", "body": "GMV 指成交总额。"}
    created = await api.post("/skills", json=body | {"references": {"terms.md": "退款不算"}})
    assert created.status_code == 201
    assert created.json()["draft"]["references"] == {"terms.md": "退款不算"}
    assert created.json()["current"] is None
    assert (await api.post("/skills", json=body)).status_code == 409

    bad = await api.put("/skills/sales-terms/draft", json={"description": "", "body": "x"})
    assert bad.status_code == 422
    assert bad.json()["detail"][0]["loc"] == ["body", "description"]

    no_note = await api.post("/skills/sales-terms/publish", json={"note": " "})
    assert no_note.status_code == 422
    published = await api.post(
        "/skills/sales-terms/publish", json={"note": "第一版", "actor": "李四"}
    )
    assert published.status_code == 200
    detail = published.json()
    assert detail["published_version"] == 1
    assert detail["draft"] is None
    assert detail["versions"][0] | {"created_at": None} == {
        "version": 1,
        "note": "第一版",
        "created_by": "李四",
        "created_at": None,
    }
    assert (await api.post("/skills/sales-terms/publish", json={"note": "x"})).status_code == 409

    version = await api.get("/skills/sales-terms/versions/1")
    assert version.json()["body"] == "GMV 指成交总额。"
    assert (await api.get("/skills/sales-terms/versions/9")).status_code == 404
    assert (await api.get("/skills/nope")).status_code == 404

    await api.put("/skills/sales-terms/draft", json={"description": "销售术语", "body": "新"})
    assert (await api.delete("/skills/sales-terms/draft")).status_code == 204
    assert (await api.delete("/skills/sales-terms/draft")).status_code == 404
