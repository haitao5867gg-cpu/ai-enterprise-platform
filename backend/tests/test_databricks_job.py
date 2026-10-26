"""Deploying a bundle as a notebook job on an existing cluster (AEP_DATABRICKS_DEPLOY=job).

The job's notebook runs on Databricks; here we check the bundle, what is
deployed and how the run's outcome is read, against the fake workspace.
"""

import ast
from pathlib import Path

import pytest
import yaml

from aep import cluster_harness
from aep.config import Settings
from aep.etl import codegen
from aep.etl.plan import EtlPlan
from tests import fake_databricks
from tests.test_databricks import BAD_BODY
from tests.test_etl import PYSPARK_BODY, SQL_QUERY, decide, make_service, plan, reply, start

ROOT = f"/Users/{fake_databricks.USER}/.bundle/shop_orders/dev/files"
PASSED = {
    "tables": [
        {
            "name": "order_facts",
            "status": "COMPLETED",
            "rows": 4761,
            "dropped": 239,
            "expectations": [{"name": "positive_quantity", "passed": 4761, "failed": 239}],
        }
    ]
}
FAILED = {
    "tables": [{"name": "order_facts", "status": "FAILED", "rows": None, "dropped": 0}],
    "errors": [
        "order_facts: AnalysisException: [UNRESOLVED_COLUMN.WITH_SUGGESTION] A column "
        "with name `o`.`qty` cannot be resolved."
    ],
}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("aep.databricks.CLUSTER_POLL_SECONDS", 0)


def job_bundle(language: str = "pyspark") -> codegen.Bundle:
    confirmed = EtlPlan.model_validate(plan(language=language))
    body = PYSPARK_BODY if language == "pyspark" else SQL_QUERY
    return codegen.render("shop_orders", confirmed, {"order_facts": body}, "示例库", job=True)


def as_job(workspace: fake_databricks.FakeWorkspace, **changes):
    return workspace.target(databricks_cluster_id=fake_databricks.CLUSTER_ID, **changes)


def test_a_cluster_makes_job_the_default():
    assert Settings().databricks_deploy_mode == "pipeline"
    assert Settings(databricks_cluster_id="c").databricks_deploy_mode == "job"
    explicit = Settings(databricks_cluster_id="c", databricks_deploy="cluster")
    assert explicit.databricks_deploy_mode == "cluster"


def test_job_bundle():
    bundle = job_bundle()

    assert codegen.check_bundle(bundle) == []
    config = yaml.safe_load(bundle.files["databricks.yml"])
    assert "pipelines" not in config["resources"]
    [task] = config["resources"]["jobs"]["shop_orders"]["tasks"]
    assert task["existing_cluster_id"] == "${var.cluster_id}"
    assert task["notebook_task"]["notebook_path"] == codegen.JOB_NOTEBOOK
    assert set(task["notebook_task"]["base_parameters"]) == {"catalog", "schema", "source_catalog"}
    assert task["max_retries"] == 1
    assert "cluster_id" in config["variables"]
    notebook = bundle.files[codegen.JOB_NOTEBOOK]
    assert notebook.startswith("# Databricks notebook source\n")
    assert 'run_notebook(spark, dbutils, "pipeline.py")' in notebook
    ast.parse(notebook)
    assert bundle.files[codegen.JOB_HARNESS] == Path(cluster_harness.__file__).read_text()
    assert "--var cluster_id=" in bundle.files["README.md"]


def test_sql_job_bundle_runs_the_sql_file():
    bundle = job_bundle("sql")

    assert codegen.check_bundle(bundle) == []
    assert 'run_notebook(spark, dbutils, "pipeline.sql")' in bundle.files[codegen.JOB_NOTEBOOK]


def test_job_bundle_must_point_at_its_notebook():
    bundle = job_bundle()
    del bundle.files[codegen.JOB_NOTEBOOK]

    assert codegen.check_bundle(bundle) == [f"databricks.yml 引用的 {codegen.JOB_NOTEBOOK} 不存在"]


async def test_deploys_the_job_runs_it_and_reads_the_outcome(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]

    dev = await as_job(workspace).run(job_bundle())

    assert dev.status == "passed", dev.errors
    assert (dev.mode, dev.stage, dev.cluster_id) == ("job", "run", fake_databricks.CLUSTER_ID)
    assert (dev.job_id, dev.run_id) == (100, 501)
    assert dev.url == f"{fake_databricks.HOST}/#job/100/run/501"
    [table] = dev.tables
    assert (table.name, table.rows, table.dropped) == ("order_facts", 4761, 239)
    assert workspace.calls == []  # no CLI

    # The files sit where bundle deploy puts them; the notebook source became a notebook.
    assert set(workspace.files) == {
        f"{ROOT}/databricks.yml",
        f"{ROOT}/README.md",
        f"{ROOT}/src/pipeline.py",
        f"{ROOT}/src/aep_harness.py",
        f"{ROOT}/src/run",
    }
    job = workspace.jobs[100]
    assert job["name"] == "[dev dev_user] shop_orders"
    assert job["tags"] == {"dev": "dev_user", "aep_bundle": "shop_orders"}
    [task] = job["tasks"]
    assert task["existing_cluster_id"] == fake_databricks.CLUSTER_ID
    assert "max_retries" not in task
    assert task["notebook_task"] == {
        "notebook_path": f"{ROOT}/src/run",
        "base_parameters": {
            "catalog": "icsd-ace-dev",
            "schema": "trac_b_test",
            "source_catalog": "icsd-ace-dev",
        },
    }


async def test_unset_schema_follows_the_dev_target(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    await as_job(workspace, databricks_schema="", databricks_catalog="").run(job_bundle())

    parameters = workspace.jobs[100]["tasks"][0]["notebook_task"]["base_parameters"]
    assert (parameters["catalog"], parameters["schema"]) == ("main", "dev_user_shop_orders")


async def test_a_second_deploy_updates_the_same_job(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    target = as_job(workspace)

    await target.run(job_bundle())
    dev = await target.run(job_bundle())

    assert list(workspace.jobs) == [100]
    assert workspace.job_runs == [100, 100]
    assert dev.run_id == 502


async def test_code_errors_in_the_job_are_fixable(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [FAILED]

    dev = await as_job(workspace).run(job_bundle())

    assert (dev.status, dev.stage, dev.fixable) == ("failed", "run", True)
    assert "UNRESOLVED_COLUMN" in dev.errors[0]
    assert dev.steps[-1].ok is False


async def test_failing_to_write_a_table_is_not_blamed_on_the_code(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [{"write_errors": ["order_facts: PERMISSION_DENIED: no MODIFY"]}]

    dev = await as_job(workspace).run(job_bundle())

    assert (dev.status, dev.fixable) == ("failed", False)
    assert "PERMISSION_DENIED" in dev.errors[0]


async def test_a_job_that_fails_before_reporting_is_not_blamed_on_the_code(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = ["ModuleNotFoundError: No module named 'aep_harness'"]

    dev = await as_job(workspace).run(job_bundle())

    assert (dev.status, dev.stage, dev.fixable) == ("failed", "run", False)
    assert "aep_harness" in dev.errors[0]


async def test_a_failed_deploy_stops_before_running(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    dev = await as_job(workspace, databricks_token="wrong").run(job_bundle())

    assert (dev.status, dev.stage, dev.fixable) == ("failed", "deploy", False)
    assert "HTTP 401" in dev.errors[0]
    assert workspace.job_runs == []


async def test_a_pipeline_bundle_is_not_deployed_as_a_job(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    confirmed = EtlPlan.model_validate(plan())
    pipeline = codegen.render("shop_orders", confirmed, {"order_facts": PYSPARK_BODY}, "示例库")

    dev = await as_job(workspace).run(pipeline)

    assert (dev.status, dev.stage) == ("failed", "deploy")
    assert "resources.jobs" in dev.errors[0]


async def test_the_cluster_and_source_catalog_are_required(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    dev = await workspace.target(databricks_deploy="job").run(job_bundle())

    assert (dev.status, dev.fixable) == ("failed", False)
    assert "AEP_DATABRICKS_CLUSTER_ID" in dev.errors[0]


async def test_failed_job_goes_back_to_codegen_and_passes(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [FAILED, PASSED]
    service, gateway, ds = await make_service(
        [
            reply(plan()),
            reply({"targets": {"order_facts": BAD_BODY}}),
            reply({"targets": {"order_facts": PYSPARK_BODY}}),
        ],
        settings=workspace.settings(databricks_cluster_id=fake_databricks.CLUSTER_ID),
        databricks=as_job(workspace),
    )

    run_id = await start(service, ds, name="shop_orders")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    retry = gateway.requests[2]["body"]["messages"][-1]["content"]
    assert "UNRESOLVED_COLUMN" in retry
    verification = view.result["verification"]
    assert (verification["status"], verification["mode"], verification["attempt"]) == (
        "passed", "job", 2,
    )  # fmt: skip
    assert codegen.JOB_NOTEBOOK in view.result["files"]
    assert 'F.col("o.quantity")' in workspace.files[f"{ROOT}/src/pipeline.py"]
    texts = [m["content"] for m in view.messages]
    assert any(t.startswith("Databricks dev 目标上的 Job 运行失败") for t in texts)
    assert "已在 Databricks dev 目标上部署并运行 Job shop_orders" in texts[-1]
    assert "写入 4761 行" in texts[-1]
