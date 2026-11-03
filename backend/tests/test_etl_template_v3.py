"""The etl-lakehouse-project template (docs/api/data-nodes.md, "应用模板 `etl-lakehouse`
第 3 版") on the fake model, Databricks connector and workspace: a run bound to a project
profiles the project's reference tables, writes a version 3 plan, runs on the project's
dev environment, checks the result (with the old system's table) and is delivered; a
failed check leaves no 通过, and a note on a check goes back to the model."""

import copy
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.config import get_settings
from aep.datasources import MemoryDataSourceStore
from aep.knowledge.store import MemoryKnowledgeStore
from aep.knowledge.worker import KnowledgeWorker
from aep.main import create_app
from aep.projects import MemoryProjectStore
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import builtin
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tools.mcp import McpTools
from tests import fake_connector, fake_databricks
from tests.conftest import FakeGateway
from tests.test_etl import reply
from tests.test_flow_data_nodes import POLICY

CATALOG, SCHEMA = "icsd-ace-dev", "jade_dev"
PROJECT: dict[str, Any] = {
    "project_id": "jade-ibf",
    "name": "IBF 预测",
    "description": "Table 2 预测",
    "table_prefix": "ibf_",
    "environments": [
        {
            "key": "dev",
            "label": "开发",
            "catalog": CATALOG,
            "schema": SCHEMA,
            "cluster_id": fake_databricks.CLUSTER_ID,
        },
        {"key": "prod", "label": "生产", "catalog": "icsd-ace-prod", "schema": "jade"},
    ],
    "references": [
        {"name": "prev.sales.forecast", "label": "预测表", "filter": "Type = 'Vol.'"},
        {"name": "prev.sales.sku", "label": "物料", "filter": None},
    ],
    "adopted": [],
    "params": [
        {
            "key": "update_month",
            "label": "统计月",
            "type": "month",
            "required": True,
            "default": "2026-10",
        }
    ],
    "policy": {"samples": None, "models": None},
}
KEYS = {
    "id": "keys",
    "label": "每个物料每月一行",
    "kind": "unique",
    "table": "ibf_table2",
    "columns": ["sku", "month"],
    # The plan says it must hold; a model's check without a severity only warns.
    "severity": "error",
}
VOLUME = {
    "id": "volume",
    "label": "数量和预测表对得上",
    "kind": "reconcile",
    "left": {"table": "ibf_table2", "expr": "sum(qty)", "where": "month = ${param.update_month}"},
    "right": {
        "table": "prev.sales.forecast",
        "expr": "sum(value)",
        "where": "month = ${param.update_month}",
    },
}


def v3_plan(**changes: Any) -> dict:
    """Table 2 of the pilot, in small: the month's volume forecast per material."""
    value: dict[str, Any] = {
        "summary": "按物料整理统计月的预测数量。",
        "language": "pyspark",
        "targets": [
            {
                "name": "ibf_table2",
                "description": "每个物料每月的预测数量",
                "sources": ["prev.sales.forecast", "prev.sales.sku"],
                "logic": "预测表只取统计月，关联物料表",
                "columns": [
                    {"name": "sku", "source": "sku", "label": "物料号"},
                    {"name": "month", "source": "month", "label": "月份"},
                    {"name": "qty", "source": "value", "label": "预测数量"},
                ],
                "primary_key": ["sku", "month"],
                "load": {"mode": "replace_where", "where": "month = ${param.update_month}"},
                "quality_rules": [
                    {"name": "qty_not_negative", "condition": "qty >= 0", "on_violation": "warn"}
                ],
            }
        ],
        "brief": {
            "goal": "按物料整理统计月的预测数量",
            "grain": "每个物料每月一行",
            "metrics": [
                {"id": "m1", "name": "预测数量", "definition": "预测表里的数量", "column": "qty"}
            ],
            "dimensions": [
                {"id": "d1", "name": "物料", "definition": "物料号", "column": "sku"},
                {"id": "d2", "name": "月份", "definition": "预测的月份", "column": "month"},
            ],
            "filters": [{"id": "f1", "text": "只取统计月"}],
            "time_range": None,
        },
        "rules": [{"id": "r1", "text": "只取统计月的预测", "basis": "data"}],
        "questions": [],
        "checks": [KEYS, VOLUME],
    }
    value.update(changes)
    return value


BODY = """
rows = source("prev.sales.forecast").where(F.col("month") == param("update_month"))
return rows.join(source("prev.sales.sku"), "sku").select(
    "sku", "month", F.col("value").alias("qty")
)
"""
GOOD_CODE = reply({"targets": {"ibf_table2": BODY}})
WRITE = {
    "mode": "replace_where",
    "version": 4,
    "operation": "WRITE",
    "rows_written": 1,
    "rows_updated": None,
    "rows_deleted": 1,
}
PASSED = {
    "tables": [
        {
            "name": "ibf_table2",
            "status": "COMPLETED",
            "rows": 1,
            "dropped": 0,
            "expectations": [{"name": "qty_not_negative", "passed": 1, "failed": 0}],
            "write": WRITE,
        }
    ]
}
PREVIEW = {
    "tables": [
        {
            "target": [CATALOG, SCHEMA, "ibf_table2"],
            "columns": ["sku", "month", "qty"],
            "rows": [{"sku": "A1", "month": "2026-10", "qty": 10}],
        }
    ]
}
COLUMNS = [
    {"name": n, "data_type": t, "nullable": True, "primary_key": False, "comment": None}
    for n, t in [("sku", "string"), ("month", "string"), ("qty", "int")]
]
GOOD_ROWS = [{"sku": "A1", "month": "2026-10", "qty": 10}]
OLD_ROWS = [{"sku": "A1", "month": "2026-10", "qty": 10}]


def put_table(monkeypatch: pytest.MonkeyPatch, schema: str, table: str, rows: list) -> None:
    monkeypatch.setitem(
        fake_connector.DATABRICKS_TABLES,
        (schema, table),
        {"comment": None, "columns": COLUMNS, "foreign_keys": [], "rows": rows},
    )


class ProjectLake:
    """One platform: the fake Databricks connector as the data source, the fake
    workspace for the dev run, a knowledge base, and the project."""

    def __init__(self, tmp_path, replies: list[str]) -> None:
        self.ws = fake_databricks.FakeWorkspace(tmp_path)
        self.settings = self.ws.settings(
            databricks_cluster_id=fake_databricks.CLUSTER_ID, knowledge_dir=tmp_path
        )
        self.gateway = FakeGateway(replies)
        self.knowledge = MemoryKnowledgeStore()
        self.sources = MemoryDataSourceStore()
        self.projects = MemoryProjectStore()
        self.calls: list[tuple[str, dict]] = []
        servers = {
            fake_connector.URL: fake_connector.build(),
            fake_connector.DATABRICKS_URL: fake_connector.build_databricks(self.calls),
        }
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            default_apps(),
            AppContext(
                self.gateway.client,
                self.settings,
                datasources=self.sources,
                tools=McpTools(5, resolve=lambda url: servers[url]),
                databricks=self.ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID),
                knowledge=self.knowledge,
                projects=self.projects,
            ),
        )
        self.catalog = AppCatalog(self.service, self.sources, self.settings)
        self.worker = KnowledgeWorker(self.knowledge, self.gateway.client, retry_delays=(0, 0))
        self.app = create_app()
        self.app.state.catalog = self.catalog
        self.app.state.run_service = self.service
        self.app.state.knowledge = self.knowledge
        self.app.state.knowledge_worker = self.worker
        self.app.state.llm_client = self.gateway.client
        self.app.state.datasources = self.sources
        self.app.state.projects = self.projects
        self.app.dependency_overrides[get_settings] = lambda: self.settings

    async def setup(self, api: httpx.AsyncClient) -> None:
        self.api = api
        lake = await self.sources.create("数据湖", "databricks", fake_connector.DATABRICKS_URL)
        self.lake = lake.datasource_id
        made = await api.post("/projects", json=PROJECT)
        assert made.status_code == 201, made.text
        made = await api.post("/knowledge-bases", json={"name": "预测口径"})
        kb_id = made.json()["kb_id"]
        uploaded = await api.post(
            f"/knowledge-bases/{kb_id}/documents",
            files={"files": ("01-口径.md", ("# 口径\n\n" + POLICY).encode())},
        )
        assert uploaded.status_code == 202, uploaded.text
        assert await self.worker.process_once()
        created = await api.post("/apps", json={"app": "ibf", "title": "IBF 预测"})
        assert created.status_code == 201, created.text
        config = copy.deepcopy(builtin.TEMPLATES["etl-lakehouse-project"].config)
        for node in config["graph"]["nodes"]:
            if node["type"] == "knowledge":
                node["knowledge_bases"] = [kb_id]
        saved = await api.put("/apps/ibf/draft", json={"config": config})
        assert saved.status_code == 200, saved.text
        published = await api.post("/apps/ibf/draft/publish", json={"note": "第 3 版"})
        assert published.status_code == 200, published.text

    async def view(self, run_id: str) -> dict:
        await self.service.wait_idle()
        return (await self.api.get(f"/runs/{run_id}")).json()

    async def start(
        self, golden: str = "old.jade.table2", params: dict[str, Any] | None = None
    ) -> tuple[str, dict]:
        made = await self.api.post(
            "/runs",
            json={
                "app": "ibf",
                "input": {"datasource": self.lake, "request": "整理 Table 2", "golden": golden},
                "project": {"project_id": "jade-ibf", "params": params or {}},
                "requester": "李雷",
            },
        )
        assert made.status_code == 201, made.text
        run_id = made.json()["run_id"]
        view = await self.view(run_id)
        assert view["status"] == "waiting", view["error"]
        return run_id, view

    async def answer(self, run_id: str, decision: str, **body: Any) -> httpx.Response:
        return await self.api.post(
            f"/runs/{run_id}/resume", json={"decision": decision, "actor": "韩梅梅"} | body
        )

    async def submit(self, run_id: str, decision: str, **body: Any) -> dict:
        resp = await self.answer(run_id, decision, **body)
        assert resp.status_code == 202, resp.text
        return await self.view(run_id)

    def prompt(self, back: int = 1) -> str:
        return self.gateway.requests[-back]["body"]["messages"][-1]["content"]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aep.databricks.POLL_SECONDS", 0)
    monkeypatch.setattr("aep.databricks.CLUSTER_POLL_SECONDS", 0)
    put_table(monkeypatch, "old.jade", "table2", OLD_ROWS)


@pytest.fixture
async def make(tmp_path):
    made: list[ProjectLake] = []

    async def build(replies: list[str], jobs: list[dict], commands: list[dict]) -> ProjectLake:
        platform = ProjectLake(tmp_path, replies)
        platform.ws.job_outputs = jobs
        platform.ws.cluster_outputs = commands
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=platform.app), base_url="http://test"
        )
        await platform.setup(client)
        made.append(platform)
        return platform

    yield build
    for platform in made:
        await platform.service.wait_idle()
        await platform.api.aclose()


async def test_a_bound_run_is_planned_checked_and_delivered(make, monkeypatch):
    put_table(monkeypatch, f"{CATALOG}.{SCHEMA}", "ibf_table2", GOOD_ROWS)
    lake = await make([reply(v3_plan()), GOOD_CODE], [PASSED], [PREVIEW])
    run_id, view = await lake.start()

    # The project's reference tables were profiled, the forecast through its filter.
    # The tables are profiled concurrently, so in no particular order.
    profiled = [a.get("where") or "" for t, a in lake.calls if t == "table_stats"]
    assert sorted(profiled) == ["", "Type = 'Vol.'"]
    prompt = lake.gateway.requests[0]["body"]["messages"][-1]["content"]
    assert "## 项目：IBF 预测" in prompt and "`prev.sales.forecast`" in prompt
    system = lake.gateway.requests[0]["body"]["messages"][0]["content"]
    assert '"checks"' in system and "replace_where" in system

    pending = view["pending"]
    assert view["step"]["node"] == "review", view["error"]
    show = pending["details"]["show"]
    assert "- 写表方式：只替换满足 `month = ${param.update_month}` 的行" in show
    assert "### 数据检查" in show and "**每个物料每月一行**" in show
    data = pending["details"]["data"]
    assert [c["id"] for c in data["plan"]["checks"]] == ["keys", "volume"]

    view = await lake.submit(run_id, "approve", seen=pending["details"]["seen"])
    assert view["step"]["node"] == "data_review", view["error"]
    ingest = [m["content"] for m in view["messages"] if m["node"] == "ingest"]
    assert ingest[0].startswith("这 2 张表本来就在 Databricks 上，直接读，不复制")
    # The code was written with the project's brief; the job ran in its environment.
    assert "## 项目：IBF 预测" in lake.prompt()
    [job] = lake.ws.jobs.values()
    parameters = job["tasks"][0]["notebook_task"]["base_parameters"]
    assert (parameters["catalog"], parameters["schema"]) == (CATALOG, SCHEMA)
    assert parameters["project"] == "jade-ibf"

    # The checks read the result in the environment, the forecast through its filter,
    # and compared it with the old system's table on the primary key.
    checked = {t: a for t, a in lake.calls if t in ("duplicates", "aggregate", "compare")}
    assert checked["duplicates"]["table"] == f"{CATALOG}.{SCHEMA}.ibf_table2"
    assert checked["aggregate"]["where"] == "(Type = 'Vol.') AND (month = '2026-10')"
    assert (checked["compare"]["expected"], checked["compare"]["keys"]) == (
        "old.jade.table2",
        ["sku", "month"],
    )
    pending = view["pending"]
    assert pending["options"] == ["approve", "fix_code", "fix_plan", "stop"]
    data = pending["details"]["data"]
    assert [(c["id"], c["status"]) for c in data["checks"]] == [
        ("keys", "passed"),
        ("volume", "passed"),
        ("golden", "passed"),
    ]
    assert data["checks_summary"] == "3 项检查：3 项通过"
    assert "## 数据检查" in pending["details"]["show"]

    view = await lake.submit(run_id, "approve")
    assert view["step"]["node"] == "release_review", view["error"]
    pending = view["pending"]
    assert pending["options"] == ["release", "skip"]
    assert pending["fields"][0]["only_for"] == ["release"]
    # The release brief: where it goes, the scheduled runs' parameters, how to undo it.
    show = pending["details"]["show"]
    assert "## 上线说明" in show and "`icsd-ace-prod.jade`" in show
    assert "| 统计月（`update_month`） | 用默认值 | 2026-10 |" in show
    assert "### 怎么撤销" in show
    view = await lake.submit(run_id, "release", values={"schedule": "0 0 2 1 * ?"})
    assert view["status"] == "done", view["error"]
    assert view["outcome"] == "delivered"
    assert view["result"]["checks"].startswith("## 数据检查\n\n3 项检查：3 项通过")
    assert view["project"]["project_id"] == "jade-ibf"

    # Released to the project's prod environment, as a job of its own on a schedule.
    assert view["result"]["release"] == (
        "已上线到 prod：Job 101，每月 1 日 02:00（Asia/Shanghai）刷新 `icsd-ace-prod`.jade"
    )
    assert view["result"]["release_url"] == f"{fake_databricks.HOST}/#job/101"
    prod = lake.ws.jobs[101]
    assert prod["name"] == "ibf_table2（jade-ibf/prod）"
    assert prod["tags"] == {
        "aep_bundle": "ibf_table2",
        "aep_target": "prod",
        "aep_project": "jade-ibf",
        "aep_environment": "prod",
    }
    assert prod["schedule"]["quartz_cron_expression"] == "0 0 2 1 * ?"
    task = prod["tasks"][0]
    # The environment names no cluster: the platform's.
    assert task["existing_cluster_id"] == fake_databricks.CLUSTER_ID
    parameters = task["notebook_task"]["base_parameters"]
    assert (parameters["catalog"], parameters["schema"]) == ("icsd-ace-prod", "jade")
    assert (parameters["project"], parameters["run_id"]) == ("jade-ibf", "{{job.run_id}}")
    assert "/.bundle/ibf_table2/prod/releases/" in task["notebook_task"]["notebook_path"]


async def test_a_failed_check_cannot_be_passed_and_a_note_on_it_goes_back(make, monkeypatch):
    repeated = GOOD_ROWS + [{"sku": "A1", "month": "2026-10", "qty": 0}]
    put_table(monkeypatch, f"{CATALOG}.{SCHEMA}", "ibf_table2", repeated)
    lake = await make(
        [reply(v3_plan()), GOOD_CODE, GOOD_CODE], [PASSED, PASSED], [PREVIEW, PREVIEW]
    )
    run_id, view = await lake.start(golden="")
    view = await lake.submit(run_id, "approve", seen=view["pending"]["details"]["seen"])
    pending = view["pending"]
    assert view["step"]["node"] == "data_review", view["error"]
    # The dev run passed; the repeated key fails a check, so 通过 is not offered.
    assert pending["details"]["data"]["status"] == "passed"
    assert pending["options"] == ["fix_code", "fix_plan", "stop"]
    keys = pending["details"]["data"]["checks"][0]
    assert (keys["id"], keys["status"]) == ("keys", "failed")
    assert keys["rows"] == [{"sku": "A1", "month": "2026-10", "count": 2}]
    # No old system's table: no golden comparison.
    assert [c["id"] for c in pending["details"]["data"]["checks"]] == ["keys", "volume"]
    refused = await lake.answer(run_id, "approve")
    assert refused.status_code == 422

    put_table(monkeypatch, f"{CATALOG}.{SCHEMA}", "ibf_table2", GOOD_ROWS)
    note = {"item": "check:keys", "text": "同一个物料同一个月只能有一行"}
    view = await lake.submit(run_id, "fix_code", notes=[note])
    assert view["status"] == "waiting", view["error"]
    said = "【第 1 轮 · 审核试算结果 · 改代码】数据检查「每个物料每月一行」："
    said += "同一个物料同一个月只能有一行"
    assert said in lake.prompt()
    pending = view["pending"]
    assert pending["options"] == ["approve", "fix_code", "fix_plan", "stop"]
    assert pending["details"]["data"]["checks_summary"] == "2 项检查：2 项通过"


async def test_a_failed_dev_run_skips_the_checks(make, monkeypatch):
    put_table(monkeypatch, f"{CATALOG}.{SCHEMA}", "ibf_table2", GOOD_ROWS)
    failed = {"write_errors": ["ibf_table2: 写表失败：没有权限"]}
    lake = await make([reply(v3_plan()), GOOD_CODE], [failed], [])
    run_id, view = await lake.start()
    profiled = len(lake.calls)  # The profile counts the filtered reference's rows.
    view = await lake.submit(run_id, "approve", seen=view["pending"]["details"]["seen"])
    pending = view["pending"]
    assert view["step"]["node"] == "data_review", view["error"]
    assert pending["options"] == ["fix_code", "fix_plan", "stop"]
    data = pending["details"]["data"]
    assert (data["status"], data["checks"]) == ("failed", [])
    assert data["checks_summary"] == "前一步没有通过，没有做数据检查"
    checked = [t for t, _ in lake.calls[profiled:] if t in ("duplicates", "aggregate", "compare")]
    assert not checked


def test_template_follows_the_contract():
    template = builtin.TEMPLATES["etl-lakehouse-project"]
    assert (template.title, template.needs) == (
        "数据 ETL（湖仓，绑定项目）",
        ("datasource", "knowledge_base"),
    )
    old = builtin.TEMPLATES["etl-lakehouse"].config["graph"]
    ids = [n["id"] for n in old["nodes"]]
    nodes = {n["id"]: n for n in template.config["graph"]["nodes"]}
    # Version 2's nodes, with the data check before the review and whether the project
    # can release before the release approval; version 2 is unchanged.
    at, then = ids.index("data_review"), ids.index("release_review")
    assert list(nodes) == [*ids[:at], "checks", *ids[at:then], "can_release", *ids[then:]]
    assert "checks" not in ids and "can_release" not in ids
    start = nodes["start"]
    assert start["project"] == "required"
    assert [(i["key"], i.get("advanced", False)) for i in start["inputs"]] == [
        ("datasource", False),
        ("request", False),
        ("tables", True),
        ("golden", True),
    ]
    assert nodes["plan"]["skills"] == [{"name": "etl-plan-project", "version": 1}]
    assert "{{project.brief}}" in nodes["plan"]["prompt"]
    assert nodes["codegen"]["skills"] == [{"name": "etl-codegen-project", "version": 1}]
    assert nodes["check_plan"]["format"] == 3 and "language" not in nodes["check_plan"]
    assert "name" not in nodes["check_code"]
    checks = nodes["checks"]
    assert (checks["type"], checks["only_if"], checks["include"]) == (
        "data_check",
        "{{dev_run.ok}}",
        ["{{project.checks}}", "{{check_plan.checks}}"],
    )
    review = nodes["data_review"]
    assert review["options"][0]["requires"] == ["{{dev_run.ok}}", "{{checks.ok}}"]
    assert review["data"]["checks"] == "{{checks.results}}"
    edges = [(e["from"], e.get("branch"), e["to"]) for e in template.config["graph"]["edges"]]
    for branch in ("passed", "failed"):
        assert ("dev_run", branch, "checks") in edges
        assert ("checks", branch, "data_review") in edges
        assert ("dev_run", branch, "data_review") not in edges
    # Releasing (jadeflow retest I-19): asked only when the project has a prod environment
    # the run is not in, offered only when the scheduled runs can fill in every parameter;
    # the refresh time is asked only of someone releasing.
    assert ("data_review", "approve", "can_release") in edges
    assert ("can_release", "yes", "release_review") in edges
    assert ("can_release", "else", "end") in edges
    assert ("data_review", "approve", "release_review") not in edges
    [rule] = nodes["can_release"]["rules"]
    assert rule["conditions"] == [{"left": "{{project.can_release}}", "op": "eq", "right": True}]
    release_review = nodes["release_review"]
    assert release_review["options"][0] == {
        "key": "release",
        "label": "上线到 prod",
        "requires": "{{project.release_ready}}",
    }
    assert "{{project.release_brief}}" in release_review["show"]
    assert release_review["fields"][0]["only_for"] == ["release"]


async def test_the_release_step_follows_the_project(make, monkeypatch):
    """jadeflow retest I-19: no release step without prod; 先不上线 needs no refresh time;
    no release while a parameter of the scheduled runs cannot be filled in."""
    put_table(monkeypatch, f"{CATALOG}.{SCHEMA}", "ibf_table2", GOOD_ROWS)

    async def reviewed(params: dict[str, Any] | None = None) -> tuple[ProjectLake, str, dict]:
        lake = await make([reply(v3_plan()), GOOD_CODE], [PASSED], [PREVIEW])
        run_id, view = await lake.start(params=params)
        view = await lake.submit(run_id, "approve", seen=view["pending"]["details"]["seen"])
        assert view["step"]["node"] == "data_review", view["error"]
        return lake, run_id, await lake.submit(run_id, "approve")

    environments = PROJECT["environments"]
    monkeypatch.setitem(PROJECT, "environments", environments[:1])
    lake, run_id, view = await reviewed()
    # Only a dev environment: delivered right after the review.
    assert (view["status"], view["outcome"]) == ("done", "delivered"), view["error"]
    assert view["result"]["release"] is None
    steps = (await lake.api.get(f"/runs/{run_id}/flow")).json()["steps"]
    assert "release_review" not in [s["node"] for s in steps]

    monkeypatch.setitem(PROJECT, "environments", environments)
    month = {"key": "update_month", "label": "统计月", "type": "month", "required": True}
    monkeypatch.setitem(PROJECT, "params", [month])
    lake, run_id, view = await reviewed({"update_month": "2026-10"})
    assert view["step"]["node"] == "release_review", view["error"]
    pending = view["pending"]
    # 统计月 has no rule for scheduled runs and no default: releasing is not offered, and
    # neither is the refresh time, asked only of someone releasing.
    assert pending["options"] == ["skip"]
    assert pending["fields"] is None
    assert "**现在不能上线：**" in pending["details"]["show"]
    refused = await lake.answer(run_id, "release", values={"schedule": "0 0 2 * * ?"})
    assert refused.status_code == 422
    view = await lake.submit(run_id, "skip")
    assert (view["status"], view["outcome"]) == ("done", "delivered"), view["error"]
    assert lake.ws.jobs.keys() == {100}  # only the dev job
