"""The etl app end to end on the fake connector, with scripted model replies."""

import ast
import io
import json
import zipfile
from dataclasses import asdict
from uuid import uuid4

import httpx
import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from aep import llm
from aep.datasources import MemoryDataSourceStore
from aep.etl import codegen
from aep.etl.plan import EtlPlan, KnownValues, PlanError, compared_literals, validate_plan
from aep.etl.profile import describe_for_model, known_values
from aep.knowledge.citations import IndexedHit
from aep.knowledge.search import search
from aep.knowledge.store import Chunk, Document
from aep.main import app
from aep.runtime import appconfig
from aep.runtime.apps import etl
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService, retryable
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
    assert view.record.step == {"node": "review", "title": "确认方案"}
    assert view.pending["details"]["type"] == "etl_plan"
    assert view.pending["details"]["plan"]["targets"][0]["load"]["mode"] == "incremental"
    profiled = gateway.requests[0]["body"]["messages"][1]["content"]
    assert "sales.orders" in profiled and "空值率" in profiled
    assert '取值分布（样本）："Paid" 1，"paid" 1' in profiled

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


async def test_plan_assumptions_are_shown_for_confirmation():
    guess = "数据里没有“已完成”状态，按 lower(trim(status)) = 'paid' 统计"
    service, _, ds = await make_service([reply(plan(assumptions=[guess]))])

    run_id = await start(service, ds)

    view = await service.get(run_id)
    assert view.pending["details"]["plan"]["assumptions"] == [guess]
    assert f"请确认这些假设：\n- {guess}" in view.messages[-1]["content"]


async def reference_material(service, gateway):
    """Three distinct chunks with matching mock embeddings, all above the score cutoff."""
    store = service.knowledge
    base = await store.create("建表约定")
    document = Document(str(uuid4()), base.kb_id, "建表.md", "md", 100, "/unused.md")
    await store.add_documents(base.kb_id, [document])
    claimed = await store.claim()
    text = "做一张订单明细宽表"
    [vector] = await llm.embed(gateway.client, [text], "mock-embed")
    await store.replace_chunks(
        claimed,
        [
            Chunk(
                str(uuid4()),
                document.document_id,
                base.kb_id,
                i,
                text,
                heading=f"规则 {i}",
                embedding=vector,
            )
            for i in range(3)
        ],
    )
    config = appconfig.default_config(etl.CONFIG)
    config["nodes"]["plan"]["params"].update(knowledge_bases=[base.kb_id], knowledge_top_k=3)
    return config


async def test_plan_references_come_from_actual_citations_and_survive_confirmation():
    proposed = plan(
        summary="订单宽表[3]，保留依据[1]；不支持的标记[0][4][999]。",
        assumptions=["按约定[1][3]。"],
    )
    proposed["targets"][0]["description"] = "目标表说明[3][5]"
    proposed["targets"][0]["columns"][0]["description"] = "订单标识[1][99]"
    # The model cannot inject references, even malformed ones.
    proposed["references"] = "模型伪造的出处"
    service, gateway, ds = await make_service(
        [reply(proposed), reply({"targets": {"order_facts": PYSPARK_BODY}})]
    )
    config = await reference_material(service, gateway)
    record = await service.create(
        "etl", {"datasource_id": ds, "request": "做一张订单明细宽表"}, config=config
    )
    await service.wait_idle()
    view = await service.get(record.run_id)
    assert view.record.status == "waiting", view.record.error
    actual = view.pending["details"]["plan"]
    assert [r["index"] for r in actual["references"]] == [1, 3]
    assert actual["summary"] == "订单宽表[3]，保留依据[1]；不支持的标记。"
    assert actual["assumptions"] == ["按约定[1][3]。"]
    assert actual["targets"][0]["description"] == "目标表说明[3]"
    assert actual["targets"][0]["columns"][0]["description"] == "订单标识[1]"
    assert all(r["document_name"] == "建表.md" for r in actual["references"])
    prompt = gateway.requests[0]["body"]["messages"]
    assert "references" not in prompt[0]["content"]
    assert all(f"[{n}]《建表.md》" in prompt[1]["content"] for n in (1, 2, 3))
    assert "没有资料依据的内容不要编号" in prompt[1]["content"]
    done = await decide(service, record.run_id, "approve")
    assert done.record.status == "done", done.record.error
    assert done.result["plan"]["references"] == actual["references"]
    assert done.sources == []  # Code apps do not use the flow run's sources.


@pytest.mark.parametrize("material", ["none", "empty", "ready"])
async def test_plan_without_valid_citations_has_no_references(material):
    service, gateway, ds = await make_service([reply(plan(summary="方案[99]"))])
    config = None
    if material == "ready":
        config = await reference_material(service, gateway)
    elif material == "empty":
        base = await service.knowledge.create("空的资料库")
        config = appconfig.default_config(etl.CONFIG)
        config["nodes"]["plan"]["params"]["knowledge_bases"] = [base.kb_id]
    record = await service.create(
        "etl", {"datasource_id": ds, "request": "做一张订单明细宽表"}, config=config
    )
    await service.wait_idle()
    view = await service.get(record.run_id)
    assert view.record.status == "waiting", view.record.error
    assert view.pending["details"]["plan"]["references"] == []
    assert view.pending["details"]["plan"]["summary"] == "方案"


@pytest.mark.parametrize("failure", ["missing_base", "gateway"])
async def test_plan_search_failure_only_warns_in_the_plan_flow_step(monkeypatch, failure):
    service, gateway, ds = await make_service([reply(plan(summary="方案[1]"))])
    config = await reference_material(service, gateway)
    if failure == "missing_base":
        await service.knowledge.delete(config["nodes"]["plan"]["params"]["knowledge_bases"][0])
    else:

        async def fail(*args, **kwargs):
            raise ValueError("向量模型不可用")

        monkeypatch.setattr("aep.runtime.apps.etl.search", fail)
    record = await service.create(
        "etl", {"datasource_id": ds, "request": "做一张订单明细宽表"}, config=config
    )
    await service.wait_idle()
    view = await service.get(record.run_id)
    assert view.record.status == "waiting", view.record.error
    proposed = view.pending["details"]["plan"]
    assert proposed["references"] == [] and proposed["summary"] == "方案"
    flow = await service.flow(record.run_id)
    step = next(s for s in flow["steps"] if s["node"] == "plan")
    assert step["status"] == "done"
    assert "知识库检索失败，继续生成方案" in step["output"]["warnings"][0]


async def test_plan_citations_in_each_supported_description_field():
    service, gateway, _ = await make_service([reply(plan())])
    config = await reference_material(service, gateway)
    hits = await search(
        service.knowledge,
        gateway.client,
        config["nodes"]["plan"]["params"]["knowledge_bases"],
        "做一张订单明细宽表",
        3,
    )
    references = [IndexedHit(**asdict(hit), index=i) for i, hit in enumerate(hits, 1)]
    for field in ("summary", "assumptions", "table", "column"):
        raw = plan()
        if field == "summary":
            raw["summary"] = "说明[2][9]"
        elif field == "assumptions":
            raw["assumptions"] = ["假设[2][9]"]
        elif field == "table":
            raw["targets"][0]["description"] = "说明[2][9]"
        else:
            raw["targets"][0]["columns"][0]["description"] = "说明[2][9]"
        result = validate_plan(raw, ["sales.orders", "sales.customers"], references=references)
        assert [r.index for r in result.references] == [2]
        assert "[9]" not in json.dumps(result.model_dump())


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
    service, _, ds = await make_service([reply(plan())])

    run_id = await start(service, "missing")

    record = (await service.get(run_id)).record
    assert "数据源" in record.error
    # Trying again would fail the same way: a copy with another input is the way on.
    assert record.error_type == "input_error"
    assert not retryable(record)
    bad_name = (await service.get(await start(service, ds, tables=["orders"]))).record
    assert (bad_name.error, bad_name.error_type) == ("表 orders 要写成 schema.table", "input_error")


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
    "condition",
    ["COUNT(*) = COUNT(DISTINCT order_id)", "amount <= max (amount)", "sum(quantity) > 0"],
)
def test_quality_rules_cannot_aggregate(condition):
    bad = plan()
    bad["targets"][0]["quality_rules"] = [
        {"name": "order_id_unique", "condition": condition, "on_violation": "warn"}
    ]
    with pytest.raises(PlanError, match="order_id_unique 用了聚合函数"):
        validate_plan(bad, ["sales.orders", "sales.customers"])


def test_row_level_rules_pass():
    good = plan()
    good["targets"][0]["quality_rules"] = [
        {"name": "q", "condition": "greatest(quantity, 0) = quantity", "on_violation": "drop"}
    ]
    validate_plan(good, ["sales.orders", "sales.customers"])


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


def test_compared_literals():
    assert compared_literals("count_if(lower(trim(o.status)) = 'returned')") == [
        ("status", "returned")
    ]
    assert compared_literals("CASE WHEN status <> 'it''s' THEN 1 END") == [("status", "it''s")]
    assert sorted(compared_literals("Status NOT IN ('a', 'b') AND kind == 'c'")) == [
        ("kind", "c"),
        ("status", "a"),
        ("status", "b"),
    ]
    assert compared_literals("ordered_at >= '2026-01-01'") == []


def returns_plan(assumptions=()):
    value = plan(assumptions=list(assumptions))
    value["targets"][0]["columns"].append(
        {"name": "returned", "source": "count_if(lower(trim(o.status)) = 'returned')"}
    )
    return value


@pytest.mark.parametrize(("complete", "where"), [(True, "整张表"), (False, "样本")])
def test_a_value_the_data_lacks_goes_back_until_explained(complete, where):
    tables = ["sales.orders", "sales.customers"]
    known = {"status": KnownValues(frozenset({"paid", "cancelled"}), complete)}
    with pytest.raises(PlanError, match=f"{where}里 status 的取值只有 cancelled、paid"):
        validate_plan(returns_plan(), tables, known)
    explained = returns_plan(["源库没有退货状态，按用户说的 returned 统计"])
    # The first answer goes back even when explained, so the model sees the actual values.
    with pytest.raises(PlanError, match="'returned'"):
        validate_plan(explained, tables, known, trust_assumptions=False)
    validate_plan(explained, tables, known)

    fine = plan()
    fine["targets"][0]["quality_rules"] = [
        {"name": "paid_only", "condition": "lower(status) IN (' Paid ')", "on_violation": "drop"}
    ]
    validate_plan(fine, tables, known, trust_assumptions=False)


def test_values_a_target_computes_are_its_own():
    tables = ["sales.orders", "sales.customers"]
    known = {"status": KnownValues(frozenset({"paid"}), complete=True)}
    relabel = "CASE WHEN lower(trim(o.status)) = 'paid' THEN 'completed' ELSE 'other' END"
    renamed = plan()
    first = renamed["targets"][0]
    first["columns"].append({"name": "status", "source": relabel})
    first["quality_rules"] = [
        {"name": "known_status", "condition": "status IN ('completed', 'other')"}
    ]
    renamed["targets"].append(
        {
            "name": "status_counts",
            "sources": ["order_facts"],
            "logic": "按状态计数",
            "columns": [{"name": "completed", "source": "count_if(status = 'completed')"}],
        }
    )
    validate_plan(renamed, tables, known, trust_assumptions=False)

    copied = plan()
    copied["targets"][0]["columns"].append({"name": "status", "source": "lower(trim(o.status))"})
    copied["targets"][0]["quality_rules"] = [
        {"name": "not_returned", "condition": "status <> 'returned'"}
    ]
    with pytest.raises(PlanError, match="'returned'"):
        validate_plan(copied, tables, known, trust_assumptions=False)

    # Downstream of a copy the input still holds the source's values, whatever the
    # downstream target makes of them.
    rewritten = plan()
    rewritten["targets"][0]["columns"].append({"name": "status", "source": "o.status"})
    rewritten["targets"].append(
        {
            "name": "relabelled",
            "sources": ["order_facts"],
            "logic": "改写状态",
            "columns": [
                {
                    "name": "status",
                    "source": "CASE WHEN status = 'returned' THEN 'completed' ELSE status END",
                }
            ],
        }
    )
    with pytest.raises(PlanError, match="relabelled 拿 status 和 'returned' 比较"):
        validate_plan(rewritten, tables, known, trust_assumptions=False)


def test_known_values_from_the_profile():
    def table(rows, limit, columns):
        return {"sample_row_count": rows, "sample_limit": limit, "column_stats": columns}

    def values(*listed, **extra):
        return [{"value": v, "count": 1, **extra} for v in listed]

    known = known_values(
        [
            table(
                3,
                1000,
                [
                    {"name": "status", "distinct_count": 2, "top_values": values("Paid", "x ")},
                    {
                        "name": "note",
                        "distinct_count": 1,
                        "top_values": values("a", truncated=True),
                    },
                    {"name": "city", "distinct_count": 30},
                    {"name": "kind", "distinct_count": 3, "top_values": values("a", "b")},
                ],
            ),
            table(
                1000, 1000, [{"name": "Status", "distinct_count": 1, "top_values": values("new")}]
            ),
        ]
    )
    assert known == {"status": KnownValues(frozenset({"paid", "x", "new"}), complete=False)}
    alone = known_values([table(3, 1000, [{"name": "s", "distinct_count": 0, "top_values": []}])])
    assert alone == {"s": KnownValues(frozenset(), complete=True)}


def test_the_profile_text_says_when_its_stats_come_from_a_sample():
    def table(rows):
        return {
            "name": "retail.customers",
            "estimated_row_count": 1200,
            "sample_row_count": rows,
            "sample_limit": 1000,
            "columns": [{"name": "customer_id", "data_type": "bigint", "primary_key": True}],
            "column_stats": [{"name": "customer_id", "null_fraction": 0, "distinct_count": rows}],
            "foreign_keys": [],
            "sample": [],
        }

    sampled = describe_for_model([table(1000)])
    assert sampled.startswith(
        "表 retail.customers（约 1200 行；空值率、不同值和取值分布只统计了 1000 行样本）\n"
    )
    assert describe_for_model([table(900)]).startswith("表 retail.customers（约 1200 行）\n")


async def test_a_made_up_value_goes_back_to_the_model():
    service, gateway, ds = await make_service([reply(returns_plan()), reply(plan())])
    run_id = await start(service, ds, tables=["sales.orders", "sales.customers"])
    view = await service.get(run_id)
    assert view.record.status == "waiting", view.record.error
    feedback = gateway.requests[1]["body"]["messages"][-1]["content"]
    assert "整张表里 status 的取值只有 paid。" in feedback
