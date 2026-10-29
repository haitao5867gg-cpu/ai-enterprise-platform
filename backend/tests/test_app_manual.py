"""GET /apps/{app}/manual and GET /runs/{id}/manual: what an app is made of."""

import httpx
import pytest

from aep.runtime import manual
from aep.runtime.apps import etl
from tests.test_flow_data_nodes import Lakehouse, lakehouse

RUNTIME_SOURCE = "运行时选择（开始节点的「数据源」）"


@pytest.fixture
async def lake(tmp_path):
    platform = Lakehouse(tmp_path, ["你好"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=platform.app), base_url="http://test"
    ) as api:
        await platform.setup(api)
        yield platform
    await platform.service.wait_idle()


def ref(*ids: str) -> list[dict[str, str]]:
    titles = {n["id"]: n["title"] for n in lakehouse("ds", "kb")["graph"]["nodes"]}
    return [{"id": i, "title": titles[i]} for i in ids]


async def test_flow_app_manual_lists_steps_and_what_they_use(lake):
    resp = await lake.api.get("/apps/retail-etl/manual")
    assert resp.status_code == 200, resp.text
    m = resp.json()
    assert (m["app"], m["title"], m["kind"], m["version"]) == (
        "retail-etl",
        "零售数据 ETL",
        "flow",
        2,
    )
    steps = {s["id"]: s for s in m["steps"]}
    # Breadth first from the start node; edges back to a listed node are not followed.
    assert list(steps) == [
        "start", "profile", "glossary", "plan", "check_plan", "review",
        "ingest", "codegen", "check_code", "dev_run", "end",
    ]  # fmt: skip
    assert steps["profile"] | {"uses": None} == {
        "id": "profile",
        "title": "探查数据",
        "type": "etl_profile",
        "type_label": "探查数据",
        "kind": "tool",
        "note": "先看真实数据。",
        "uses": None,
    }
    assert steps["glossary"]["note"] is None
    assert steps["profile"]["uses"] == [f"数据源 {RUNTIME_SOURCE}"]
    assert steps["glossary"]["uses"] == ["知识库 零售数据口径"]
    assert steps["plan"]["uses"] == ["Skill etl-plan v1", "模型 mock"]
    assert steps["ingest"]["uses"] == [f"数据源 {RUNTIME_SOURCE}", "Databricks"]
    assert steps["dev_run"]["uses"] == ["Databricks"]
    assert steps["check_plan"]["uses"] == [] and steps["check_plan"]["kind"] == "code"

    assert [(s["name"], s["version"], s["nodes"]) for s in m["skills"]] == [
        ("etl-plan", 1, ref("plan")),
        ("etl-codegen", 1, ref("codegen")),
    ]
    assert m["skills"][0]["description"]
    [base] = m["knowledge_bases"]
    assert base["kb_id"] == lake.kb_id and base["name"] == "零售数据口径"
    assert base["document_count"] == 1
    assert [(d["filename"], d["status"]) for d in base["documents"]] == [
        ("01-有效订单定义.md", "ready")
    ]
    assert base["nodes"] == ref("glossary")
    assert m["datasources"] == [
        {
            "datasource_id": None,
            "name": RUNTIME_SOURCE,
            "kind": None,
            "nodes": ref("profile", "ingest"),
        }
    ]
    assert m["models"] == [{"alias": "mock", "nodes": ref("plan", "codegen")}]
    [databricks] = m["systems"]
    assert databricks["key"] == "databricks" and databricks["nodes"] == ref("ingest", "dev_run")
    assert databricks["detail"] == (
        "工作区 adb-1.test.databricks.azure.cn；源表 catalog icsd-ace-dev；"
        "结果写到 icsd-ace-dev.trac_b_test；运行方式 notebook Job（已有集群）；"
        "集群 0101-000000-abcdefgh"
    )
    assert "dapi-test-token" not in resp.text
    assert m["human"] == [{"id": "review", "title": "确认方案", "prompt": "确认这个方案吗？"}]


async def test_manual_of_a_version_a_draft_and_a_run(lake):
    assert (await lake.api.get("/apps/retail-etl/manual?draft=true")).status_code == 404
    assert (await lake.api.get("/apps/retail-etl/manual?version=9")).status_code == 404
    assert (await lake.api.get("/apps/nope/manual")).status_code == 404
    first = (await lake.api.get("/apps/retail-etl/manual?version=1")).json()
    assert first["version"] == 1 and [s["id"] for s in first["steps"]][0] == "start"

    config = lakehouse(lake.ds, lake.kb_id)
    config["graph"]["nodes"][1]["datasource"] = lake.ds
    saved = await lake.api.put("/apps/retail-etl/draft", json={"config": config})
    assert saved.status_code == 200, saved.text
    draft = (await lake.api.get("/apps/retail-etl/manual?draft=true")).json()
    assert draft["version"] is None
    assert draft["datasources"][0] == {
        "datasource_id": lake.ds,
        "name": "示例库",
        "kind": "postgres",
        "nodes": ref("profile"),
    }

    made = await lake.api.post("/runs", json={"app": "retail-etl", "input": {"request": "x"}})
    run_id = made.json()["run_id"]
    await lake.service.wait_idle()
    from_run = (await lake.api.get(f"/runs/{run_id}/manual")).json()
    assert (
        from_run["version"] == 2
        and from_run["steps"] == (await lake.api.get("/apps/retail-etl/manual")).json()["steps"]
    )
    assert (await lake.api.get("/runs/nope/manual")).status_code == 404


async def test_deleted_resources_are_still_listed(lake):
    # The API refuses while an app uses it; an older version may still name it.
    await lake.knowledge.delete(lake.kb_id)
    config = lakehouse("ds_gone", lake.kb_id)
    config["graph"]["nodes"][1]["datasource"] = "ds_gone"
    config["graph"]["nodes"][3]["skills"] = [{"name": "etl-plan", "version": 7}]
    lookups = manual.Lookups(
        lake.service.skills,
        lake.knowledge,
        lake.sources,
        lake.settings.model_copy(update={"databricks_token": ""}),
        "company",
    )
    m = await manual.flow_manual({"app": "x"}, config, lookups)
    [skill] = [s for s in m["skills"] if s["name"] == "etl-plan"]
    assert skill["description"] == "已删除"
    [base] = m["knowledge_bases"]
    assert (base["name"], base["description"], base["documents"]) == (lake.kb_id, "已删除", [])
    assert m["datasources"][0]["name"] == "ds_gone（已删除）"
    assert m["models"][0]["alias"] == "company"
    assert m["systems"][0]["detail"] == "未配置（AEP_DATABRICKS_HOST、AEP_DATABRICKS_TOKEN）"


async def test_code_app_manual_follows_its_nodes(lake):
    m = (await lake.api.get("/apps/etl/manual")).json()
    assert (m["kind"], m["version"], m["title"]) == ("code", 1, etl.DESIGN.title)
    steps = {s["id"]: s for s in m["steps"]}
    assert list(steps) == list(etl.NODES)
    assert steps["plan"] | {"uses": None} == {
        "id": "plan",
        "title": "出方案",
        "type": None,
        "type_label": "模型生成",
        "kind": "model",
        "note": etl.NODES["plan"].summary,
        "uses": None,
    }
    assert steps["plan"]["uses"] == ["Skill etl-plan v1", "模型 mock"]
    assert steps["profile"]["uses"] == ["数据源 运行时选择（运行时填写的数据源）"]
    assert steps["verify"]["uses"] == ["Databricks"]
    assert steps["publish"]["uses"] == ["资产仓库（GitHub）"]
    assert [h["id"] for h in m["human"]] == ["review", "release"]
    assert {s["key"] for s in m["systems"]} == {"databricks", "assets"}

    config = {"nodes": {"plan": {"params": {"knowledge_bases": [lake.kb_id]}}}}
    saved = await lake.api.put("/apps/etl/draft", json={"config": config})
    assert saved.status_code == 200, saved.text
    draft = (await lake.api.get("/apps/etl/manual?draft=true")).json()
    assert draft["knowledge_bases"][0]["nodes"] == [{"id": "plan", "title": "出方案"}]
    assert "知识库 零售数据口径" in {s["id"]: s for s in draft["steps"]}["plan"]["uses"]
