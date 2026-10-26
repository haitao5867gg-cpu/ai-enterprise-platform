"""Releasing an approved bundle to the prod target, and the release approval in the etl app."""

import yaml
from langgraph.checkpoint.memory import InMemorySaver

from aep.config import Settings
from aep.datasources import MemoryDataSourceStore
from aep.etl import codegen
from aep.etl.plan import EtlPlan
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tools.mcp import McpTools
from tests import fake_connector, fake_databricks
from tests.conftest import FakeGateway
from tests.test_databricks_job import PASSED, job_bundle
from tests.test_etl import PYSPARK_BODY, decide, make_service, plan, reply, start

RELEASES = f"/Users/{fake_databricks.USER}/.bundle/shop_orders/prod/releases/"
PROD = {"databricks_prod_catalog": "icsd-ace-prod", "databricks_prod_schema": "sales_mart"}


def as_prod_job(workspace: fake_databricks.FakeWorkspace, **changes):
    return workspace.target(databricks_cluster_id=fake_databricks.CLUSTER_ID, **PROD | changes)


def test_release_needs_a_prod_target_and_a_deploying_mode():
    ready = {"databricks_host": "https://h", "databricks_token": "t", **PROD}
    assert Settings(**ready).databricks_release_blocker is None
    assert "工作区" in str(Settings(**PROD).databricks_release_blocker)
    no_prod = Settings(databricks_host="https://h", databricks_token="t")
    assert "AEP_DATABRICKS_PROD_CATALOG" in str(no_prod.databricks_release_blocker)
    cluster = Settings(**ready, databricks_cluster_id="c", databricks_deploy="cluster")
    assert "cluster 模式" in str(cluster.databricks_release_blocker)


def test_bundles_schedule_only_the_prod_target():
    config = yaml.safe_load(job_bundle().files["databricks.yml"])
    prod = config["targets"]["prod"]
    schedule = prod["resources"]["jobs"]["shop_orders"]["schedule"]
    assert schedule["quartz_cron_expression"] == "${var.schedule}"
    assert config["variables"]["schedule"]["default"] == codegen.DEFAULT_SCHEDULE
    assert "resources" not in config["targets"]["dev"]
    assert "schedule" not in config["resources"]["jobs"]["shop_orders"]

    confirmed = EtlPlan.model_validate(plan())
    pipeline = codegen.render("shop_orders", confirmed, {"order_facts": PYSPARK_BODY}, "示例库")
    config = yaml.safe_load(pipeline.files["databricks.yml"])
    refresh = config["targets"]["prod"]["resources"]["jobs"]["shop_orders_refresh"]
    [task] = refresh["tasks"]
    assert task["pipeline_task"]["pipeline_id"] == "${resources.pipelines.shop_orders.id}"
    assert codegen.check_bundle(pipeline) == []


async def test_releases_the_job_to_prod_on_a_schedule_without_running_it(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    release = await as_prod_job(workspace).release(job_bundle(), schedule="0 30 6 * * ?")

    assert release.status == "deployed", release.errors
    assert (release.mode, release.job_id) == ("job", 100)
    assert (release.catalog, release.schema) == ("icsd-ace-prod", "sales_mart")
    assert release.url == f"{fake_databricks.HOST}/#job/100"
    assert workspace.job_runs == []
    job = workspace.jobs[100]
    notebook = job["tasks"][0]["notebook_task"]["notebook_path"]
    assert notebook.startswith(RELEASES) and notebook.endswith("/files/src/run")
    assert notebook in workspace.files
    assert job["name"] == "shop_orders"
    assert job["tags"] == {"aep_bundle": "shop_orders", "aep_target": "prod"}
    assert job["schedule"] == {
        "quartz_cron_expression": "0 30 6 * * ?",
        "timezone_id": "Asia/Shanghai",
        "pause_status": "UNPAUSED",
    }
    [task] = job["tasks"]
    assert task["max_retries"] == 1  # prod keeps the bundle's retries
    assert task["existing_cluster_id"] == fake_databricks.CLUSTER_ID
    assert task["notebook_task"] == {
        "notebook_path": notebook,
        "base_parameters": {
            "catalog": "icsd-ace-prod",
            "schema": "sales_mart",
            "source_catalog": "icsd-ace-dev",
        },
    }


async def test_prod_can_use_its_own_cluster_and_default_schedule(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    target = as_prod_job(workspace, databricks_prod_cluster_id="prod-cluster")
    release = await target.release(job_bundle())

    assert release.cluster_id == "prod-cluster"
    job = workspace.jobs[100]
    assert job["tasks"][0]["existing_cluster_id"] == "prod-cluster"
    assert job["schedule"]["quartz_cron_expression"] == codegen.DEFAULT_SCHEDULE


async def test_a_failed_release_says_why(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    release = await as_prod_job(workspace, databricks_token="wrong").release(job_bundle())
    assert release.status == "failed"
    assert "HTTP 401" in release.errors[0]

    unconfigured = workspace.target(databricks_cluster_id=fake_databricks.CLUSTER_ID)
    release = await unconfigured.release(job_bundle())
    assert release.status == "failed"
    assert "AEP_DATABRICKS_PROD_CATALOG" in release.errors[0]


async def test_a_failed_release_leaves_the_running_job_as_it_was(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    target = as_prod_job(workspace)
    first = await target.release(job_bundle())
    live = workspace.jobs[100]["tasks"][0]["notebook_task"]["notebook_path"]
    files = dict(workspace.files)

    changed = job_bundle()
    changed.files["src/pipeline.py"] += "\n# changed\n"
    second = await target.release(changed, schedule="bad cron")

    assert first.status == "deployed"
    assert second.status == "failed" and "HTTP 400" in second.errors[0]
    assert workspace.jobs[100]["tasks"][0]["notebook_task"]["notebook_path"] == live
    # The new files went to a folder of their own; the live ones are unchanged.
    assert {k: v for k, v in workspace.files.items() if k in files} == files

    third = await target.release(changed)
    assert (third.status, third.job_id, list(workspace.jobs)) == ("deployed", 100, [100])
    assert workspace.jobs[100]["tasks"][0]["notebook_task"]["notebook_path"] != live


async def test_never_takes_over_a_job_the_platform_did_not_deploy(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.jobs[7] = {"name": "shop_orders", "tags": {"owner": "finance"}, "tasks": []}

    release = await as_prod_job(workspace).release(job_bundle())

    assert release.status == "failed"
    assert "不是平台部署的" in release.errors[0]
    assert workspace.jobs == {7: {"name": "shop_orders", "tags": {"owner": "finance"}, "tasks": []}}
    assert workspace.files == {}  # nothing uploaded either

    # A dev job of the bundle does not count as the prod job.
    workspace.jobs = {}
    workspace.job_outputs = [PASSED]
    await as_prod_job(workspace).run(job_bundle())
    release = await as_prod_job(workspace).release(job_bundle())
    assert (release.status, sorted(workspace.jobs)) == ("deployed", [100, 101])


async def test_releases_a_pipeline_with_the_cli(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    confirmed = EtlPlan.model_validate(plan())
    bundle = codegen.render("shop_orders", confirmed, {"order_facts": PYSPARK_BODY}, "示例库")

    release = await workspace.target(**PROD).release(bundle)

    assert release.status == "deployed", release.errors
    assert (release.mode, release.pipeline_id) == ("pipeline", fake_databricks.PIPELINE_ID)
    steps = [call["args"][:2] for call in workspace.calls]
    assert steps == [["bundle", "validate"], ["bundle", "deploy"], ["bundle", "summary"]]
    deploy = workspace.calls[1]["args"]
    assert "--target=prod" in deploy
    assert "--var=catalog=icsd-ace-prod" in deploy
    assert "--var=schema=sales_mart" in deploy
    assert f"--var=schedule={codegen.DEFAULT_SCHEDULE}" in deploy
    assert not any(a.startswith("--var=cluster_id") for a in deploy)


async def test_dev_pass_waits_for_release_approval_then_deploys_to_prod(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED, PASSED]
    better = PYSPARK_BODY.replace('F.trim(F.col("c.city"))', 'F.upper(F.trim(F.col("c.city")))')
    gateway = FakeGateway(
        [
            reply(plan()),
            reply({"targets": {"order_facts": PYSPARK_BODY}}),
            reply({"targets": {"order_facts": better}}),
        ]
    )
    store, checkpoints, sources = MemoryRunStore(), InMemorySaver(), MemoryDataSourceStore()
    shop = await sources.create("示例库", "postgres", fake_connector.URL)
    connector = fake_connector.build()

    def service() -> RunService:
        # Built again after a "restart": same run store and checkpoints, new process state.
        settings = workspace.settings(databricks_cluster_id=fake_databricks.CLUSTER_ID, **PROD)
        ctx = AppContext(
            gateway.client,
            settings,
            datasources=sources,
            tools=McpTools(5, resolve=lambda url: connector),
            databricks=as_prod_job(workspace),
        )
        return RunService(store, checkpoints, default_apps(), ctx)

    first = service()
    run_id = await start(first, shop.datasource_id, name="shop_orders", schedule="0 0 5 * * ?")
    await first.resume(run_id, "approve", None, actor="张三")
    await first.wait_idle()
    view = await first.get(run_id)

    assert view.record.status == "waiting", view.record.error
    details = view.pending["details"]
    assert details["type"] == "etl_release"
    assert details["verification"]["status"] == "passed"
    assert details["target"] == {
        "mode": "job",
        "catalog": "icsd-ace-prod",
        "schema": "sales_mart",
        "schedule": "0 0 5 * * ?",
        "timezone": "Asia/Shanghai",
        "cluster_id": fake_databricks.CLUSTER_ID,
    }
    assert "release" not in view.result
    assert workspace.jobs[100]["name"].startswith("[dev ")  # only dev so far

    # Rejecting sends the code back with the comment; it passes dev again and waits again.
    view = await decide(first, run_id, "reject", "城市统一成大写")
    assert view.record.status == "waiting", view.record.error
    assert "城市统一成大写" in gateway.requests[2]["body"]["messages"][-1]["content"]
    assert view.result["verification"]["attempt"] == 1

    # A restart keeps the waiting approval.
    restarted = service()
    await restarted.resume(run_id, "approve", "可以上线", actor="李四")
    await restarted.wait_idle()
    view = await restarted.get(run_id)

    assert view.record.status == "done", view.record.error
    release = view.result["release"]
    assert (release["status"], release["job_id"]) == ("deployed", 101)
    assert workspace.jobs[101]["schedule"]["quartz_cron_expression"] == "0 0 5 * * ?"
    assert workspace.job_runs == [100, 100]  # dev ran twice, prod not at all
    approvals = [(a["step"], a["decision"], a["actor"]) for a in view.result["approvals"]]
    assert approvals == [
        ("plan", "approve", "张三"),
        ("release", "reject", None),
        ("release", "approve", "李四"),
    ]
    assert view.result["approvals"][-1]["comment"] == "可以上线"
    assert view.messages[-1]["content"].startswith("已上线到 prod：shop_orders 部署为 Job 101")


async def test_without_a_prod_target_the_run_ends_after_dev(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    service, _, ds = await make_service(
        [reply(plan()), reply({"targets": {"order_facts": PYSPARK_BODY}})],
        settings=workspace.settings(databricks_cluster_id=fake_databricks.CLUSTER_ID),
        databricks=workspace.target(databricks_cluster_id=fake_databricks.CLUSTER_ID),
    )

    run_id = await start(service, ds, name="shop_orders")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    assert view.result["release"]["status"] == "skipped"
    assert "AEP_DATABRICKS_PROD_CATALOG" in view.result["release"]["reason"]
    assert [a["step"] for a in view.result["approvals"]] == ["plan"]
