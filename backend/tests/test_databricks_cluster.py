"""Dev validation on an existing cluster (AEP_DATABRICKS_CLUSTER_ID), against the fake workspace.

The harness itself needs Spark and is checked on a real cluster; here we check
what is sent to the cluster and how its outcome is read.
"""

import json

import pytest

from aep import cluster_harness
from aep.etl import codegen
from aep.etl.plan import EtlPlan
from tests import fake_databricks
from tests.test_databricks import BAD_BODY, bundle
from tests.test_etl import PYSPARK_BODY, SQL_QUERY, decide, make_service, plan, reply, start

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
    monkeypatch.setattr("aep.databricks.POLL_SECONDS", 0)
    monkeypatch.setattr("aep.databricks.CLUSTER_POLL_SECONDS", 0)


def on_cluster(workspace: fake_databricks.FakeWorkspace):
    return workspace.target(databricks_cluster_id=fake_databricks.CLUSTER_ID)


def sent_spec(command: str) -> dict:
    call = command.rsplit("\n", 1)[1]
    assert call.startswith("main(spark, json.loads(")
    return json.loads(json.loads(call.removeprefix("main(spark, json.loads(").removesuffix("))")))


async def test_runs_the_transformations_on_the_cluster(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_outputs = [PASSED]

    dev = await on_cluster(workspace).run(bundle())

    assert dev.status == "passed", dev.errors
    assert (dev.mode, dev.cluster_id) == ("cluster", fake_databricks.CLUSTER_ID)
    assert dev.url == f"{fake_databricks.HOST}/compute/clusters/{fake_databricks.CLUSTER_ID}"
    [table] = dev.tables
    assert (table.name, table.rows, table.dropped) == ("order_facts", 4761, 239)
    assert workspace.calls == []  # nothing deployed
    assert workspace.contexts == set()  # the execution context is cleaned up

    [command] = workspace.commands
    assert command["language"] == "python"
    compile(command["command"], "command", "exec")
    assert command["command"].startswith(open(cluster_harness.__file__).read())
    spec = sent_spec(command["command"])
    assert spec["language"] == "pyspark" and spec["source_catalog"] == "icsd-ace-dev"
    assert spec["code"] == bundle().files[codegen.PIPELINE_PY]


async def test_sql_targets_are_sent_parsed(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_outputs = [PASSED]
    confirmed = EtlPlan.model_validate(plan(language="sql"))
    sql = codegen.render("shop_orders", confirmed, {"order_facts": SQL_QUERY}, "示例库")

    dev = await on_cluster(workspace).run(sql)

    assert dev.status == "passed"
    [target] = sent_spec(workspace.commands[0]["command"])["targets"]
    assert target["name"] == "order_facts"
    assert "FROM `${source_catalog}`.sales.orders o" in target["query"]
    assert target["rules"] == [
        {"name": "positive_quantity", "condition": "quantity > 0", "on_violation": "drop"}
    ]


async def test_a_stopped_cluster_is_started(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_state = "TERMINATED"
    workspace.cluster_outputs = [PASSED]

    dev = await on_cluster(workspace).run(bundle())

    assert dev.status == "passed"
    assert "已启动" in dev.warnings[0]
    starts = [r for r in workspace.requests if r.url.path.endswith("/clusters/start")]
    assert len(starts) == 1


async def test_code_errors_on_the_cluster_are_fixable(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_outputs = [FAILED]

    dev = await on_cluster(workspace).run(bundle())

    assert (dev.status, dev.stage, dev.fixable) == ("failed", "run", True)
    assert "UNRESOLVED_COLUMN" in dev.errors[0]


async def test_a_broken_command_is_not_blamed_on_the_code(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_outputs = ["PERMISSION_DENIED: cannot attach to cluster"]

    dev = await on_cluster(workspace).run(bundle())

    assert (dev.status, dev.fixable) == ("failed", False)
    assert "PERMISSION_DENIED" in dev.errors[0]
    assert workspace.contexts == set()


async def test_an_unknown_cluster_is_reported(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    dev = await workspace.target(databricks_cluster_id="nope").run(bundle())

    assert (dev.status, dev.fixable) == ("failed", False)
    assert "HTTP 400" in dev.errors[0]


async def test_failed_cluster_run_goes_back_to_codegen_and_passes(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.cluster_outputs = [FAILED, PASSED]
    service, gateway, ds = await make_service(
        [
            reply(plan()),
            reply({"targets": {"order_facts": BAD_BODY}}),
            reply({"targets": {"order_facts": PYSPARK_BODY}}),
        ],
        settings=workspace.settings(databricks_cluster_id=fake_databricks.CLUSTER_ID),
        databricks=on_cluster(workspace),
    )

    run_id = await start(service, ds, name="shop_orders")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    retry = gateway.requests[2]["body"]["messages"][-1]["content"]
    assert "UNRESOLVED_COLUMN" in retry
    verification = view.result["verification"]
    assert (verification["status"], verification["mode"], verification["attempt"]) == (
        "passed", "cluster", 2,
    )  # fmt: skip
    texts = [m["content"] for m in view.messages]
    assert any(t.startswith(f"在集群 {fake_databricks.CLUSTER_ID} 上试跑失败") for t in texts)
    assert "没有部署管道" in texts[-1] and "得到 4761 行" in texts[-1]


def test_sql_pipeline_parses_back_into_its_targets():
    confirmed = EtlPlan.model_validate(
        plan(
            language="sql",
            targets=[
                plan()["targets"][0]
                | {
                    "description": "it's",
                    "quality_rules": [
                        {"name": "q1", "condition": "quantity > (0)", "on_violation": "drop"},
                        {"name": "q2", "condition": "city IN ('a', 'b')", "on_violation": "fail"},
                        {"name": "q3", "condition": "city IS NOT NULL", "on_violation": "warn"},
                    ],
                },
                {
                    "name": "city_orders",
                    "sources": ["order_facts"],
                    "logic": "按城市汇总",
                    "columns": [{"name": "city", "source": "city"}],
                },
            ],
        )
    )
    queries = {
        "order_facts": SQL_QUERY + "\nWHERE o.status <> ';'",
        "city_orders": "SELECT city, count(*) AS orders FROM order_facts GROUP BY city;",
    }

    targets = codegen.parse_sql(codegen.render_sql(confirmed, queries))

    assert [t["name"] for t in targets] == ["order_facts", "city_orders"]
    assert targets[0]["query"].endswith("WHERE o.status <> ';'")
    assert [r["on_violation"] for r in targets[0]["rules"]] == ["drop", "fail", "warn"]
    assert targets[0]["rules"][1]["condition"] == "city IN ('a', 'b')"
    assert targets[1] == {
        "name": "city_orders",
        "query": "SELECT city, count(*) AS orders FROM order_facts GROUP BY city",
        "rules": [],
    }


async def test_the_source_catalog_is_required(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    target = workspace.target(
        databricks_cluster_id=fake_databricks.CLUSTER_ID, databricks_source_catalog=""
    )
    dev = await target.run(bundle())

    assert (dev.status, dev.fixable) == ("failed", False)
    assert "AEP_DATABRICKS_SOURCE_CATALOG" in dev.errors[0]
    assert workspace.commands == []
