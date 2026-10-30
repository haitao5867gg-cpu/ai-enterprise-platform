"""Dev validation on Databricks, against the fake workspace in tests/fake_databricks.py."""

import yaml

from aep.databricks import DevTarget
from aep.etl import codegen
from aep.etl.plan import EtlPlan
from tests import fake_databricks
from tests.conftest import SETTINGS
from tests.test_etl import PYSPARK_BODY, decide, make_service, plan, reply, start

BAD_BODY = PYSPARK_BODY.replace('F.col("o.quantity")', 'F.col("o.qty")')


def bundle() -> codegen.Bundle:
    confirmed = EtlPlan.model_validate(plan())
    return codegen.render("shop_orders", confirmed, {"order_facts": PYSPARK_BODY}, "示例库")


async def test_deploys_runs_and_reads_the_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("AEP_LLM_GATEWAY_KEY", "sk-should-not-leak")
    workspace = fake_databricks.FakeWorkspace(tmp_path, runs=[True])

    dev = await workspace.target().run(bundle())

    assert dev.status == "passed", dev.errors
    assert [c["args"][1] for c in workspace.calls] == ["validate", "deploy", "summary", "run"]
    target = ["--target=dev", "--var=catalog=icsd-ace-dev", "--var=schema=trac_b_test"]
    assert workspace.calls[0]["args"][2:5] == target
    assert "--var=source_catalog=icsd-ace-dev" in workspace.calls[0]["args"]
    assert workspace.calls[-1]["args"][-1] == "shop_orders"
    assert workspace.calls[1]["files"] == ["README.md", "databricks.yml", "src/pipeline.py"]
    env = workspace.calls[0]["env"]
    assert env["DATABRICKS_HOST"] == fake_databricks.HOST
    assert env["DATABRICKS_TOKEN"] == "dapi-test-token"
    assert not [k for k in env if k.startswith("AEP_")]

    assert dev.pipeline_id == "pipe-1" and dev.update_id == "update-1"
    assert dev.url == f"{fake_databricks.HOST}/pipelines/pipe-1"
    [table] = dev.tables
    assert (table.name, table.status, table.rows, table.dropped) == (
        "order_facts", "COMPLETED", 4990, 10,
    )  # fmt: skip
    assert table.expectations == [{"name": "positive_quantity", "passed": 4990, "failed": 10}]
    assert dev.errors == []  # the earlier update's error is not this run's


async def test_failed_run_carries_the_event_log_errors(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path, runs=[False])

    dev = await workspace.target().run(bundle())

    assert (dev.status, dev.stage, dev.fixable) == ("failed", "run", True)
    assert any("UNRESOLVED_COLUMN" in e and e.startswith("order_facts: ") for e in dev.errors)
    assert dev.tables[0].status == "FAILED"


async def test_failed_deploy_is_not_blamed_on_the_code(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.fail("deploy", "Error: PERMISSION_DENIED: User does not have CREATE SCHEMA")

    dev = await workspace.target().run(bundle())

    assert (dev.status, dev.stage, dev.fixable) == ("failed", "deploy", False)
    assert "PERMISSION_DENIED" in dev.errors[0]
    assert [c["args"][1] for c in workspace.calls] == ["validate", "deploy"]


async def test_missing_cli_is_reported(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)

    dev = await workspace.target(databricks_cli=str(tmp_path / "nope")).run(bundle())

    assert (dev.status, dev.stage) == ("failed", "validate")
    assert "无法启动 Databricks CLI" in dev.errors[0]


def test_off_without_host_and_token():
    assert DevTarget.from_settings(SETTINGS) is None
    configured = SETTINGS.model_copy(update={"databricks_host": "h", "databricks_token": "t"})
    assert DevTarget.from_settings(configured) is not None


def test_classic_compute_bundle():
    confirmed = EtlPlan.model_validate(plan())
    files = codegen.render_bundle(
        "shop_orders", confirmed, "示例库", codegen.Compute("Standard_D4ds_v5", 2)
    )
    pipeline = yaml.safe_load(files["databricks.yml"])["resources"]["pipelines"]["shop_orders"]
    assert "serverless" not in pipeline
    assert pipeline["edition"] == "ADVANCED"
    assert pipeline["clusters"] == [
        {"label": "default", "node_type_id": "Standard_D4ds_v5", "num_workers": 2}
    ]


def test_sources_are_quoted_for_hyphenated_catalogs():
    pipeline = bundle().files["src/pipeline.py"]
    namespace: dict = {}
    helper = pipeline[pipeline.index("def source") : pipeline.index("\n\n\n#")]
    read: list[str] = []

    class Spark:
        class conf:  # noqa: N801
            @staticmethod
            def get(key: str) -> str:
                return "icsd-ace-dev"

        class read:  # noqa: N801
            @staticmethod
            def table(name: str) -> str:
                read.append(name)
                return name

    namespace["spark"] = Spark
    namespace["SOURCE_CATALOG"] = "icsd-ace-dev"
    exec(helper, namespace)
    namespace["source"]("sales.orders")
    assert read == ["`icsd-ace-dev`.`sales`.`orders`"]


async def dev_service(tmp_path, runs, replies):
    workspace = fake_databricks.FakeWorkspace(tmp_path, runs=runs)
    service, gateway, ds = await make_service(
        replies, settings=workspace.settings(), databricks=workspace.target()
    )
    return service, gateway, ds, workspace


async def test_failed_dev_run_goes_back_to_codegen_and_passes(tmp_path):
    service, gateway, ds, workspace = await dev_service(
        tmp_path,
        [False, True],
        [
            reply(plan()),
            reply({"targets": {"order_facts": BAD_BODY}}),
            reply({"targets": {"order_facts": PYSPARK_BODY}}),
        ],
    )

    run_id = await start(service, ds, name="shop_orders")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    retry = gateway.requests[2]["body"]["messages"][-1]["content"]
    assert "Databricks dev" in retry and "UNRESOLVED_COLUMN" in retry
    verification = view.result["verification"]
    assert (verification["status"], verification["attempt"]) == ("passed", 2)
    assert verification["tables"][0]["rows"] == 4990
    assert 'F.col("o.quantity")' in view.result["files"]["src/pipeline.py"]
    assert len([c for c in workspace.calls if c["args"][1] == "run"]) == 2
    texts = [m["content"] for m in view.messages]
    assert any(t.startswith("Databricks dev 目标运行失败") for t in texts)
    assert "已在 Databricks dev 目标上跑通 shop_orders" in texts[-1]
    assert "质量规则丢弃 10 行" in texts[-1]


async def test_dev_run_that_keeps_failing_is_reported(tmp_path):
    service, gateway, ds, workspace = await dev_service(
        tmp_path,
        [False, False, False],
        [reply(plan()), reply({"targets": {"order_facts": BAD_BODY}})],
    )

    run_id = await start(service, ds)
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done"
    verification = view.result["verification"]
    assert (verification["status"], verification["attempt"]) == ("failed", 3)
    assert view.result["checks"]["passed"] is True  # the static checks passed; the run did not
    assert len(gateway.requests) == 1 + 3
    assert "带着报错重新生成代码" not in view.messages[-1]["content"]


async def test_without_databricks_the_run_says_it_skipped(tmp_path):
    service, _, ds = await make_service(
        [reply(plan()), reply({"targets": {"order_facts": PYSPARK_BODY}})]
    )

    run_id = await start(service, ds)
    view = await decide(service, run_id, "approve")

    assert view.result["verification"]["status"] == "skipped"
    assert "AEP_DATABRICKS_HOST" in view.result["verification"]["reason"]


async def test_dropped_connections_are_retried(tmp_path, monkeypatch):
    waits: list[float] = []

    async def wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("aep.databricks.asyncio.sleep", wait)
    workspace = fake_databricks.FakeWorkspace(tmp_path, runs=[True])
    workspace.drop(5)

    dev = await workspace.target().run(bundle())

    assert dev.status == "passed" and dev.warnings == []
    assert waits[:5] == [1, 2, 4, 8, 16]
    assert dev.tables[0].rows == 4990


async def test_a_workspace_that_keeps_dropping_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr("aep.databricks.asyncio.sleep", _no_sleep)
    workspace = fake_databricks.FakeWorkspace(tmp_path, runs=[True])
    workspace.drop(100)

    dev = await workspace.target().run(bundle())

    assert dev.status == "passed"
    assert "ConnectError" in dev.warnings[0]


async def test_cluster_creation_denied_is_not_blamed_on_the_code(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.deny_clusters()

    dev = await workspace.target().run(bundle())

    assert (dev.status, dev.stage, dev.fixable) == ("failed", "run", False)
    assert dev.tables == []
    assert "not authorized to create clusters" in dev.errors[0]


async def test_cluster_creation_denied_stops_without_regenerating(tmp_path):
    service, gateway, ds, workspace = await dev_service(
        tmp_path, [], [reply(plan()), reply({"targets": {"order_facts": PYSPARK_BODY}})]
    )
    workspace.deny_clusters()

    run_id = await start(service, ds)
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done"
    verification = view.result["verification"]
    assert (verification["status"], verification["attempt"]) == ("failed", 1)
    assert len(gateway.requests) == 2  # plan + one codegen, no retry
    assert "not authorized to create clusters" in view.messages[-1]["content"]


def test_classic_compute_under_a_cluster_policy():
    confirmed = EtlPlan.model_validate(plan())
    files = codegen.render_bundle(
        "shop_orders", confirmed, "示例库", codegen.Compute(policy_id="E0631F5C0D0006C3")
    )
    pipeline = yaml.safe_load(files["databricks.yml"])["resources"]["pipelines"]["shop_orders"]
    assert pipeline["clusters"] == [
        {
            "label": "default",
            "num_workers": 1,
            "policy_id": "E0631F5C0D0006C3",
            "apply_policy_default_values": True,
        }
    ]


async def _no_sleep(seconds: float) -> None:
    return None


DESIGN = {
    "target": "order_facts",
    "filter": None,
    "group_by": ["city"],
    "measures": [{"name": "quantity", "agg": "sum"}],
    "order_by": [{"column": "quantity", "desc": True}],
    "top_n": 10,
}


async def test_without_a_cluster_the_answer_says_the_tables_cannot_be_read(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path, runs=[True, True])
    order = {"order_facts": ["order_id"]}

    dev = await workspace.target().run(bundle(), 5, order, DESIGN)
    assert dev.status == "passed"
    assert dev.answer == {"error": "没有配置集群（AEP_DATABRICKS_CLUSTER_ID），读不了结果表"}
    assert "没有配置集群（AEP_DATABRICKS_CLUSTER_ID），不预览结果数据" in dev.warnings
    assert "answer" not in dev.as_dict()

    # No preview asked for: only the answer says so.
    dev = await workspace.target().run(bundle(), 0, order, DESIGN)
    assert dev.answer is not None and not [w for w in dev.warnings if "预览" in w]
