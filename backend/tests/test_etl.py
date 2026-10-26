"""The etl app end to end on the fake connector, with scripted model replies."""

import ast
import io
import json
import zipfile

import httpx
import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from aep.datasources import MemoryDataSourceStore
from aep.etl import codegen
from aep.etl.plan import EtlPlan, PlanError, validate_plan
from aep.main import app
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tools.mcp import McpTools
from tests import fake_connector
from tests.conftest import SETTINGS, FakeGateway


def plan(**changes) -> dict:
    base = {
        "summary": "把订单和客户整理成订单明细宽表。",
        "language": "pyspark",
        "targets": [
            {
                "name": "order_facts",
                "description": "订单明细，带客户城市",
                "sources": ["sales.orders", "sales.customers"],
                "logic": "订单关联客户，城市去空格，数量为负的行丢弃",
                "columns": [
                    {"name": "order_id", "source": "o.order_id"},
                    {"name": "city", "source": "trim(c.city)"},
                    {"name": "quantity", "source": "o.quantity"},
                ],
                "primary_key": ["order_id"],
                "load": {"mode": "incremental", "key": "ordered_at"},
                "quality_rules": [
                    {
                        "name": "positive_quantity",
                        "condition": "quantity > 0",
                        "on_violation": "drop",
                    }
                ],
            }
        ],
    }
    base.update(changes)
    return base


PYSPARK_BODY = """
orders = source("sales.orders").alias("o")
customers = source("sales.customers").alias("c")
return orders.join(customers, F.col("o.customer_id") == F.col("c.customer_id")).select(
    F.col("o.order_id").alias("order_id"),
    F.trim(F.col("c.city")).alias("city"),
    F.col("o.quantity").alias("quantity"),
)
"""

SQL_QUERY = """SELECT o.order_id, trim(c.city) AS city, o.quantity
FROM sales.orders o JOIN sales.customers c ON o.customer_id = c.customer_id"""


def reply(value: dict) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"


async def make_service(replies: list[str], settings=SETTINGS, databricks=None):
    gateway = FakeGateway(replies)
    sources = MemoryDataSourceStore()
    shop = await sources.create("示例库", "postgres", fake_connector.URL)
    server = fake_connector.build()
    tools = McpTools(5, resolve=lambda url: server)
    service = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(
            gateway.client, settings, datasources=sources, tools=tools, databricks=databricks
        ),
    )
    return service, gateway, shop.datasource_id


async def start(service, datasource_id, **extra):
    inp = {"datasource_id": datasource_id, "request": "做一张订单明细宽表", **extra}
    record = await service.create("etl", inp)
    await service.wait_idle()
    return record.run_id


async def decide(service, run_id, decision, comment=None):
    await service.resume(run_id, decision, comment)
    await service.wait_idle()
    return await service.get(run_id)


async def test_request_to_bundle_with_one_rejection():
    better = plan(summary="按意见改成全量加载。")
    better["targets"][0]["load"] = {"mode": "full", "key": None}
    bad_code = {"targets": {"order_facts": 'return spark.sql("SELECT 1")'}}
    service, gateway, ds = await make_service(
        [
            reply(plan()),
            reply(better),
            reply(bad_code),
            reply({"targets": {"order_facts": PYSPARK_BODY}}),
        ]
    )

    run_id = await start(service, ds)
    view = await service.get(run_id)
    assert view.record.status == "waiting", view.record.error
    assert view.pending["details"]["type"] == "etl_plan"
    assert view.pending["details"]["plan"]["targets"][0]["load"]["mode"] == "incremental"
    profiled = gateway.requests[0]["body"]["messages"][1]["content"]
    assert "sales.orders" in profiled and "空值率" in profiled

    view = await decide(service, run_id, "reject", "先做全量")
    assert view.record.status == "waiting"
    assert view.pending["details"]["plan"]["summary"] == "按意见改成全量加载。"
    assert "先做全量" in gateway.requests[1]["body"]["messages"][-1]["content"]

    view = await decide(service, run_id, "approve")
    assert view.record.status == "done", view.record.error
    # The first code used spark.sql; the problem went back to the model.
    assert "不允许使用 .sql" in gateway.requests[3]["body"]["messages"][-1]["content"]
    result = view.result
    assert result["checks"] == {"passed": True, "problems": []}
    assert set(result["files"]) == {"databricks.yml", "README.md", "src/pipeline.py"}

    pipeline = result["files"]["src/pipeline.py"]
    ast.parse(pipeline)
    assert '@dp.materialized_view(name="order_facts"' in pipeline
    assert '@dp.expect_or_drop("positive_quantity", "quantity > 0")' in pipeline
    bundle = yaml.safe_load(result["files"]["databricks.yml"])
    assert set(bundle["targets"]) == {"dev", "prod"}
    pipeline_conf = bundle["resources"]["pipelines"][result["bundle"]]
    assert pipeline_conf["libraries"] == [{"file": {"path": "src/pipeline.py"}}]
    assert [m["role"] for m in view.messages] == [
        "user", "assistant", "assistant", "user", "assistant", "user", "assistant",
    ]  # fmt: skip


async def test_sql_pipeline_reads_sources_through_the_source_catalog():
    service, _, ds = await make_service(
        [reply(plan()), reply({"targets": {"order_facts": SQL_QUERY}})]
    )

    run_id = await start(service, ds, language="sql", name="shop_orders")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    assert view.result["bundle"] == "shop_orders"
    sql = view.result["files"]["src/pipeline.sql"]
    assert "CREATE OR REFRESH MATERIALIZED VIEW order_facts" in sql
    assert "CONSTRAINT positive_quantity EXPECT (quantity > 0) ON VIOLATION DROP ROW" in sql
    # Quoted, since catalog names may hold hyphens (icsd-ace-dev).
    assert (
        "FROM `${source_catalog}`.sales.orders o JOIN `${source_catalog}`.sales.customers c" in sql
    )


async def test_unusable_plan_is_repaired_once():
    wrong = plan()
    wrong["targets"][0]["sources"] = ["sales.invoices"]
    service, gateway, ds = await make_service([reply(wrong), reply(plan())])

    run_id = await start(service, ds)

    assert (await service.get(run_id)).record.status == "waiting"
    assert "sales.invoices" in gateway.requests[1]["body"]["messages"][-1]["content"]


async def test_plan_that_stays_unusable_fails_the_run():
    service, _, ds = await make_service(["这不是 JSON"])

    run_id = await start(service, ds)

    record = (await service.get(run_id)).record
    assert record.status == "error"
    assert "方案不可用" in record.error


async def test_code_that_never_passes_is_reported():
    service, gateway, ds = await make_service(
        [reply(plan()), reply({"targets": {"order_facts": "import os\nreturn os"}})]
    )

    run_id = await start(service, ds)
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done"
    assert view.result["checks"]["passed"] is False
    assert view.result["files"] == {}
    assert len(gateway.requests) == 1 + 3  # one plan, three code attempts


async def test_unknown_datasource_fails_clearly():
    service, _, _ = await make_service([reply(plan())])

    run_id = await start(service, "missing")

    assert "数据源" in (await service.get(run_id)).record.error


async def test_files_download_as_zip():
    service, _, ds = await make_service(
        [reply(plan()), reply({"targets": {"order_facts": PYSPARK_BODY}})]
    )
    run_id = await start(service, ds, name="shop_orders")
    await decide(service, run_id, "approve")
    app.state.run_service = service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        resp = await api.get(f"/runs/{run_id}/files.zip")
        hello = (await api.get("/runs/nope/files.zip")).status_code

    assert resp.status_code == 200
    assert 'filename="shop_orders.zip"' in resp.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert sorted(names) == ["README.md", "databricks.yml", "src/pipeline.py"]
    assert hello == 404


def test_plan_validation_names_every_problem():
    bad = plan()
    bad["targets"][0]["name"] = "Order Facts"
    bad["targets"][0]["primary_key"] = ["id"]
    bad["targets"][0]["load"] = {"mode": "incremental", "key": None}
    with pytest.raises(PlanError) as err:
        validate_plan(bad, ["sales.orders", "sales.customers"])
    message = str(err.value)
    assert "'Order Facts'" in message and "'id'" in message and "load.key" in message


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("return source('sales.orders').write", "不允许使用 .write"),
        ("return dbutils.fs.ls('/')", "不允许使用 dbutils"),
        ("return source('sales.invoices')", "不在方案列出的来源里"),
        ("return spark.read.table('sales.orders')", "只能读本管道的其他目标表"),
        ("df = source('sales.orders')", "return"),
        ("return (", "语法错误"),
    ],
)
def test_pyspark_checks(body, problem):
    problems = codegen.check_code(EtlPlan.model_validate(plan()), {"order_facts": body})
    assert any(problem in p for p in problems), problems


@pytest.mark.parametrize(
    ("query", "problem"),
    [
        ("DELETE FROM sales.orders", "SELECT"),
        ("SELECT 1; DROP TABLE x", "只能有一条语句"),
        ("WITH x AS (SELECT 1) INSERT INTO y SELECT * FROM x", "INSERT"),
    ],
)
def test_sql_checks(query, problem):
    sql_plan = EtlPlan.model_validate(plan(language="sql"))
    problems = codegen.check_code(sql_plan, {"order_facts": query})
    assert any(problem in p for p in problems), problems


def test_target_names_must_not_clash_with_generated_code():
    for name in ("source", "class"):
        bad = plan()
        bad["targets"][0]["name"] = name
        with pytest.raises(PlanError, match="保留字"):
            validate_plan(bad, ["sales.orders", "sales.customers"])
