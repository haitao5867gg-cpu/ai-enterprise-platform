"""The ETL eval runner: the eval set parses, checks score as documented, and a whole
case runs through the Runs API (fake model and workspace) without going to prod."""

import json
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep import evals
from aep.datasources import MemoryDataSourceStore
from aep.main import app
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tools.mcp import McpTools
from tests import fake_connector, fake_databricks
from tests.conftest import FakeGateway
from tests.test_databricks_job import PASSED
from tests.test_etl import PYSPARK_BODY, plan, reply
from tests.test_release import PROD

CASES = Path(__file__).parents[2] / "evals" / "etl" / "cases.yaml"


def test_the_eval_set_parses():
    cases = evals.load_cases(CASES)

    assert len(cases) >= 5
    known = {*evals.PLAN_CHECKS, "rows"}
    for case in cases:
        assert set(case.get("expect") or {}) <= known, case["id"]


def test_load_cases_rejects_duplicates(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text("- {id: a, request: x}\n- {id: a, request: y}\n")

    with pytest.raises(ValueError, match="重复"):
        evals.load_cases(path)


def by_name(checks):
    return {c.name: c for c in checks}


def test_plan_checks():
    proposed = plan(assumptions=["数据里没有已完成状态，按 paid 统计"])
    proposed["targets"].append(
        {
            "name": "daily",
            "sources": ["order_facts"],
            "logic": "按天汇总",
            "columns": [{"name": "order_date", "source": "to_date(ordered_at)"}],
            "load": {"mode": "full"},
        }
    )
    expect = {
        "sources": ["sales.orders", "sales.products"],
        "targets": {"min": 2, "max": 2},
        "layered": True,
        "columns": ["ORDER_ID", "city|town", "amount"],
        "mention": ["trim"],
        "forbid": ["returned", "o.quantity"],
        "assumptions": ["paid", "退货"],
        "load": {"mode": "incremental", "key": "ordered_at"},
    }

    checks = by_name(evals.score_plan(expect, proposed))

    assert checks["sources"].passed is False and "sales.products" in checks["sources"].detail
    assert checks["targets"].passed and checks["layered"].passed
    assert checks["columns"].passed is False and "amount" in checks["columns"].detail
    assert checks["mention"].passed
    assert checks["forbid"].passed is False and checks["forbid"].detail == "用了 ['o.quantity']"
    assert checks["assumptions"].passed is False and "退货" in checks["assumptions"].detail
    assert checks["load"].passed
    assert all(c.passed is None for c in evals.score_plan(expect, None))


def test_forbid_looks_at_what_the_tables_compute():
    honest = plan()
    target = honest["targets"][0]
    target["logic"] = "没有退货数据，returned_count 留空"
    target["columns"].append({"name": "returned_count", "source": "cast(null as bigint)"})
    honest["assumptions"] = ["数据里没有 returned 状态"]
    made_up = plan()
    made_up["targets"][0]["quality_rules"] = [
        {"name": "not_returned", "condition": "status <> 'returned'", "on_violation": "drop"}
    ]
    honest["targets"][0]["quality_rules"] = [
        {"name": "fewer_returns", "condition": "returned_count <= quantity", "on_violation": "warn"}
    ]
    missing_column = plan()
    missing_column["targets"][0]["quality_rules"] = [
        {"name": "flagged", "condition": "return_flag IS NOT NULL", "on_violation": "drop"}
    ]
    expect = {"forbid": ["returned", "return_flag"]}

    assert by_name(evals.score_plan(expect, honest))["forbid"].passed
    assert by_name(evals.score_plan(expect, made_up))["forbid"].passed is False
    assert by_name(evals.score_plan(expect, missing_column))["forbid"].passed is False


def finished(verification, **result):
    body = {
        "plan": plan(),
        "checks": {"passed": True, "problems": []},
        "verification": verification,
    }
    return {"status": "done", "result": body | result}


def test_result_checks():
    rows = [{"columns": ["order_id"], "min": 4000, "max": 5000}, {"columns": ["month"], "min": 1}]
    expect = {"rows": rows}

    checks = evals.score_result(expect, finished(PASSED | {"status": "passed"}))
    assert [(c.layer, c.name, c.passed) for c in checks] == [
        ("code", "checks", True),
        ("result", "dev_run", True),
        ("result", "rows ['order_id']", True),
        ("result", "rows ['month']", False),
    ]
    assert checks[2].detail == "order_facts 4761 行"

    skipped = evals.score_result(expect, finished({"status": "skipped", "reason": "没有工作区"}))
    assert [c.passed for c in skipped] == [True, None, None, None]

    failed = evals.score_result(expect, {"status": "error", "error": "方案不可用"})
    assert [c.passed for c in failed] == [False, None, None, None]
    assert failed[0].detail == "方案不可用"


def test_score_and_report():
    good = evals.CaseResult("a", status="done", checks=[evals.Check("plan", "x", True)])
    half = evals.CaseResult(
        "b",
        status="done",
        checks=[
            evals.Check("plan", "x", True),
            evals.Check("result", "dev_run", False, "boom"),
            evals.Check("result", "rows", None, "没有工作区"),
        ],
    )

    assert half.score == 0.5
    assert evals.summary([good, half]) == {
        "score": 0.75,
        "layers": {"plan": 1.0, "result": 0.0},
        "cases": 2,
    }
    text = evals.render([good, half], "company")
    assert "总分 75%" in text and "result.dev_run（boom）" in text and "没有工作区" in text
    assert json.loads(json.dumps(evals.report([good, half], "company")))["score"] == 0.75


async def test_runs_a_case_through_the_api_up_to_dev_only(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "POLL_SECONDS", 0)
    monkeypatch.setattr("aep.databricks.CLUSTER_POLL_SECONDS", 0)
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    gateway = FakeGateway([reply(plan()), reply({"targets": {"order_facts": PYSPARK_BODY}})])
    sources = MemoryDataSourceStore()
    await sources.create("示例库", "postgres", fake_connector.URL)
    connector = fake_connector.build()
    settings = workspace.settings(databricks_cluster_id=fake_databricks.CLUSTER_ID, **PROD)
    ctx = AppContext(
        gateway.client,
        settings,
        datasources=sources,
        tools=McpTools(5, resolve=lambda url: connector),
        databricks=workspace.target(databricks_cluster_id=fake_databricks.CLUSTER_ID, **PROD),
    )
    app.state.run_service = RunService(MemoryRunStore(), InMemorySaver(), default_apps(), ctx)
    app.state.datasources = sources
    case = {
        "id": "order-details",
        "request": "做一张订单明细宽表",
        "expect": {
            "sources": ["sales.orders"],
            "columns": ["order_id", "city"],
            "rows": [{"columns": ["order_id"], "min": 4000, "max": 5000}],
        },
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        [result] = await evals.Runner(api, model="mock").run([case])

    assert result.status == "done", result.error
    assert result.score == 1.0, [c for c in result.checks if not c.passed]
    body = gateway.requests[0]["body"]
    assert body["model"] == "mock"
    view = await app.state.run_service.get(result.run_id)
    assert view.result["release"] == {"status": "skipped", "reason": "这次运行只试跑，不上线"}
    assert view.result["approvals"][0]["actor"] == evals.ACTOR
    assert list(workspace.jobs) == [100]  # the dev job only
