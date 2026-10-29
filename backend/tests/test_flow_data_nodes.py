"""Data-engineering nodes of flow apps (docs/api/data-nodes.md): each node on the fake
connector and the fake Databricks workspace, a lakehouse ETL flow from request to
dev run, the relaxed loop rule and node notes."""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep import cluster_tasks
from aep.config import get_settings
from aep.databricks import DevRun, full_name, spark_type
from aep.datasources import MemoryDataSourceStore
from aep.etl import codegen
from aep.etl.plan import EtlPlan
from aep.knowledge.store import MemoryKnowledgeStore
from aep.knowledge.worker import KnowledgeWorker
from aep.main import create_app
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import data_nodes, files
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow.nodes import TYPES, NodeFailure, Runtime
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tools.mcp import McpTools
from tests import fake_connector, fake_databricks
from tests.conftest import SETTINGS, FakeGateway
from tests.test_etl import PYSPARK_BODY, plan, reply

ORDERS = fake_connector.TABLES[("sales", "orders")]["rows"]
CATALOG = "icsd-ace-dev"
HIT = {
    "kb_id": "kb-1",
    "kb_name": "零售数据口径",
    "document_id": "doc-1",
    "document_name": "01-有效订单定义.md",
    "chunk_id": "chunk-1",
    "position": 0,
    "text": "有效订单是状态为 paid 的订单。",
    "heading": "有效订单",
    "page": None,
    "score": 0.9,
    "scores": {"vector": 0.9, "keyword": None, "rerank": None},
    "index": 1,
}


async def node_env(databricks=None, settings=SETTINGS) -> tuple[Runtime, str]:
    sources = MemoryDataSourceStore()
    shop = await sources.create("示例库", "postgres", fake_connector.URL)
    server = fake_connector.build()
    rt = Runtime(
        FakeGateway().client,
        "mock",
        {},
        {},
        datasources=sources,
        tools=McpTools(5, resolve=lambda url: server),
        settings=settings,
        databricks=databricks,
    )
    return rt, shop.datasource_id


def state(**variables) -> dict:
    return {"input": {}, "vars": variables, "run_id": "run-1"}


async def run(node: dict, st: dict, rt: Runtime):
    return await TYPES[node["type"]].run(node, st, rt)


def profile_node(**changes) -> dict:
    return {
        "id": "profile",
        "type": "etl_profile",
        "title": "探查数据",
        "datasource": "{{start.datasource}}",
        "tables": "{{start.tables}}",
    } | changes


async def profiled(rt: Runtime, ds: str) -> list[dict]:
    out = await run(profile_node(), state(start={"datasource": ds, "tables": ""}), rt)
    return out.outputs["tables"]


# etl_profile


async def test_profile_reads_the_tables_asked_for():
    rt, ds = await node_env()
    out = await run(profile_node(), state(start={"datasource": ds, "tables": "sales.*"}), rt)
    assert out.branch is None
    assert out.outputs["source_id"] == ds
    assert out.outputs["source_name"] == "示例库"
    assert out.outputs["names"] == ["sales.customers", "sales.orders"]
    assert out.outputs["tables"][1]["sample"] == ORDERS
    assert "sales.orders" in out.outputs["description"] and "空值率" in out.outputs["description"]
    assert out.outputs["summary"] == "已探查 示例库 的 2 张表：sales.customers、sales.orders"
    assert out.messages == [{"role": "assistant", "content": out.outputs["summary"]}]

    for tables, names in [
        ("sales.orders，sales.customers", ["sales.orders", "sales.customers"]),
        ("sales.orders\nsales.orders", ["sales.orders"]),
        (["sales.orders"], ["sales.orders"]),
        ("", ["sales.customers", "sales.orders"]),
        (None, ["sales.customers", "sales.orders"]),
    ]:
        st = state(start={"datasource": ds, "tables": tables})
        assert (await run(profile_node(), st, rt)).outputs["names"] == names


async def test_profile_refuses_what_it_cannot_read(monkeypatch):
    rt, ds = await node_env()
    monkeypatch.setattr(data_nodes, "MAX_TABLES", 1)
    for tables, problem, kind in [
        ("sales.*", "一共 2 张表，一次最多 1 张", "input_error"),
        ("orders", "orders 要写成 schema.table 或 schema.*", "input_error"),
        ("sales.nope", "探查数据源「示例库」失败", "tool_error"),
    ]:
        with pytest.raises(NodeFailure) as failed:
            await run(profile_node(), state(start={"datasource": ds, "tables": tables}), rt)
        assert failed.value.type == kind
        assert problem in failed.value.message
    with pytest.raises(NodeFailure, match="没有数据源"):
        await run(profile_node(), state(start={"datasource": "nope", "tables": ""}), rt)


# etl_plan_check


def check_node(**changes) -> dict:
    return {
        "id": "check_plan",
        "type": "etl_plan_check",
        "title": "校验方案",
        "reply": "{{plan.text}}",
        "profile": "{{profile.tables}}",
        "hits": ["{{glossary.hits}}"],
        "language": "{{start.language}}",
        "max_attempts": 3,
    } | changes


async def test_plan_check_passes_a_good_plan_and_writes_it_up_for_people():
    rt, ds = await node_env()
    tables = await profiled(rt, ds)
    good = plan(summary="把订单整理成明细宽表 [1]，引用不存在的 [7] 会被去掉。")
    st = state(
        plan={"text": reply(good)},
        profile={"tables": tables},
        glossary={"hits": [HIT, HIT]},
        start={"language": "sql"},
    )
    out = await run(check_node(), st, rt)
    assert out.branch == "ok"
    assert out.private == {"failures": 0}
    o = out.outputs
    assert o["ok"] is True and o["problems"] == ""
    assert o["plan"]["language"] == "sql"
    assert o["plan"]["summary"] == "把订单整理成明细宽表 [1]，引用不存在的  会被去掉。"
    assert [r["index"] for r in o["plan"]["references"]] == [1]
    assert o["sources"] == ["sales.customers", "sales.orders"]
    assert o["targets"] == ["order_facts"]
    for text in [
        "**方案**：把订单整理成明细宽表 [1]",
        "代码语言：SQL",
        "### 目标表 1：`order_facts`",
        "- 加载方式：增量（增量键 `ordered_at`）",
        "- 来源：`sales.orders`、`sales.customers`",
        "| `city` | `trim(c.city)` |   |",
        "- `positive_quantity`：`quantity > 0`（不满足时丢弃这一行）",
        "### 依据的资料",
        "- [1]《01-有效订单定义.md》 有效订单（知识库「零售数据口径」）",
    ]:
        assert text in o["message"]
    # The run's messages only say it is ready: the person reviews ``message``.
    assert out.messages == [{"role": "assistant", "content": "方案整理好了：1 张目标表"}]


async def test_plan_check_sends_problems_back_and_gives_up_after_max_attempts():
    rt, ds = await node_env()
    tables = await profiled(rt, ds)

    def attempt(text: str, private: dict | None = None) -> dict:
        st = state(plan={"text": text}, profile={"tables": tables}, glossary={"hits": []})
        st["branch"] = {"check_plan": "retry"} if private else {}
        st["private"] = {"check_plan": private} if private else {}
        return st

    first = await run(check_node(), attempt("这不是 JSON"), rt)
    assert first.branch == "retry"
    assert first.private == {"failures": 1}
    assert first.outputs["ok"] is False and first.outputs["plan"] is None
    assert first.outputs["problems"].startswith("回复不是要求的 JSON")
    assert first.outputs["sources"] == [] and first.outputs["targets"] == []
    assert first.outputs["message"].startswith("方案没有通过平台校验，带着问题让模型重写")
    assert first.messages[0]["content"] == "模型没按要求的 JSON 格式回答，让模型重写（第 2/3 次）"

    wrong = plan()
    wrong["targets"][0]["sources"] = ["sales.missing", "sales.orders"]
    wrong["targets"][0]["primary_key"] = ["nope"]
    second = await run(check_node(), attempt(reply(wrong), first.private), rt)
    assert second.private == {"failures": 2}
    assert second.outputs["plan"]["targets"][0]["sources"][0] == "sales.missing"
    assert second.outputs["problems"].splitlines() == [
        "order_facts 引用了不存在的源表 'sales.missing'",
        "order_facts 的主键 'nope' 不在目标列里",
    ]
    assert second.messages[0]["content"] == "方案有 2 处没通过校验，让模型重写（第 3/3 次）"

    with pytest.raises(NodeFailure) as failed:
        await run(check_node(), attempt(reply(wrong), second.private), rt)
    assert failed.value.type == "output_format"
    assert "方案连续 3 次没有通过检查" in failed.value.message

    # A pass (say before a person sends the plan back) starts the count again.
    passed = await run(check_node(), attempt(reply(plan()), second.private), rt)
    assert passed.branch == "ok" and passed.private == {"failures": 0}


async def test_plan_check_takes_assumptions_at_their_word_only_after_one_retry():
    rt, ds = await node_env()
    tables = await profiled(rt, ds)
    refunds = plan(assumptions=["status 为 refunded 表示退款，数据里暂时没有"])
    refunds["targets"][0]["columns"].append(
        {"name": "refunded", "source": "CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END"}
    )
    st = state(plan={"text": reply(refunds)}, profile={"tables": tables}, glossary={"hits": []})
    first = await run(check_node(), st, rt)
    assert first.branch == "retry"
    assert "'refunded'" in first.outputs["problems"]
    st |= {"branch": {"check_plan": "retry"}, "private": {"check_plan": first.private}}
    passed = await run(check_node(), st, rt)
    assert passed.branch == "ok"
    assert passed.messages[0]["content"] == "方案整理好了：1 张目标表、1 条假设待确认"


async def test_plan_check_needs_the_profile():
    rt, _ = await node_env()
    st = state(plan={"text": reply(plan())}, profile={"tables": None}, glossary={"hits": []})
    with pytest.raises(NodeFailure, match="探查结果要引用探查数据节点的 tables"):
        await run(check_node(), st, rt)


# databricks_ingest


def ingest_node(ds: str, **changes) -> dict:
    return {
        "id": "ingest",
        "type": "databricks_ingest",
        "title": "抽取到湖仓",
        "datasource": ds,
        "tables": "{{check_plan.sources}}",
    } | changes


def sent_spec(command: str, function: str) -> dict:
    """The spec a command sent to the cluster calls ``function`` with."""
    found = re.search(rf"^{function}\(spark, json\.loads\((.*)\)\)$", command, re.M)
    assert found, command[-500:]
    return json.loads(json.loads(found[1]))


def loaded(*tables: tuple[str, int]) -> dict:
    return {"tables": [{"target": [CATALOG, "sales", t], "rows": n} for t, n in tables]}


async def test_ingest_copies_the_tables_into_the_source_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(data_nodes, "PAGE", 2)
    ws = fake_databricks.FakeWorkspace(tmp_path)
    ws.cluster_outputs = [loaded(("orders", 3), ("customers", 3)), loaded(("orders", 3))]
    rt, ds = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))
    st = state(check_plan={"sources": ["sales.orders", "sales.customers"]})
    out = await run(ingest_node(ds), st, rt)

    assert out.outputs["catalog"] == CATALOG
    assert out.outputs["tables"] == [
        {
            "source": "sales.orders",
            "target": "`icsd-ace-dev`.sales.orders",
            "rows": 3,
            "columns": 5,
        },
        {
            "source": "sales.customers",
            "target": "`icsd-ace-dev`.sales.customers",
            "rows": 3,
            "columns": 3,
        },
    ]
    assert out.outputs["summary"] == (
        "已把 2 张表共 6 行抽取到 Databricks：`` `icsd-ace-dev`.sales.orders ``（3 行）、"
        "`` `icsd-ace-dev`.sales.customers ``（3 行）"
    )
    assert ws.catalog_objects == {
        "schemas:icsd-ace-dev.sales",
        "volumes:icsd-ace-dev.sales.aep_landing",
    }
    # Every page was read (3 rows in pages of 2) and landed as JSON Lines, in a folder
    # of this call's own, removed once the tables were loaded.
    (folder,) = {path.rsplit("/", 1)[0] for path in ws.volume_files}
    assert folder.startswith(f"/Volumes/icsd-ace-dev/sales/aep_landing/{ds}/run-1-")
    landed = ws.volume_files[f"{folder}/orders.jsonl"]
    assert [json.loads(line) for line in landed.decode().splitlines()] == ORDERS
    assert ws.volume_deletes == [f"{folder}/orders.jsonl", f"{folder}/customers.jsonl", folder]

    command = ws.commands[0]["command"]
    assert command.startswith(Path(cluster_tasks.__file__).read_text())
    spec = sent_spec(command, "ingest")
    orders = spec["tables"][0]
    assert orders["path"] == f"{folder}/orders.jsonl"
    assert orders["target"] == [CATALOG, "sales", "orders"]
    assert orders["columns"] == [
        ["order_id", "BIGINT"],
        ["customer_id", "BIGINT"],
        ["quantity", "INT"],
        ["status", "STRING"],
        ["ordered_at", "TIMESTAMP"],
    ]
    assert orders["comment"].startswith("由 AEP 从数据源「示例库」抽取，运行 run-1，")
    assert orders["properties"] == {"aep.datasource_id": ds, "aep.run_id": "run-1"}
    # The command carries names and settings, never row values or the token.
    assert "a@example.com" not in command and "dapi-test-token" not in command

    # Again: the schema and volume are there now, the files are replaced.
    creates = sum(1 for r in ws.requests if r.method == "POST" and "unity-catalog" in r.url.path)
    st = state(check_plan={"sources": "sales.orders"})
    await run(ingest_node(ds), st, rt)
    assert creates == 2
    assert creates == sum(
        1 for r in ws.requests if r.method == "POST" and "unity-catalog" in r.url.path
    )


async def test_ingest_over_the_row_limit_writes_nothing(tmp_path):
    ws = fake_databricks.FakeWorkspace(tmp_path)
    rt, ds = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))
    st = state(check_plan={"sources": ["sales.customers", "sales.orders"]})
    with pytest.raises(NodeFailure) as failed:
        await run(ingest_node(ds, max_rows=2), st, rt)
    assert failed.value.type == "input_error"
    assert "sales.customers 超过 2 行，没有抽取任何表" in failed.value.message
    assert ws.requests == []


async def test_an_input_error_is_not_tried_again(tmp_path):
    """Over the row limit is decided by the data: on_error.retry would read it again."""
    ws = fake_databricks.FakeWorkspace(tmp_path)
    rt, ds = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))
    calls: list[str] = []
    call = rt.call

    async def counted(source, tool, args):
        calls.append(tool)
        return await call(source, tool, args)

    rt.call = counted
    retried = {"on_error": {"retry": {"max": 2, "interval_seconds": 0}, "then": "stop"}}
    node = ingest_node(ds, max_rows=2) | retried
    with pytest.raises(flowgraph.FlowError) as failed:
        await flowgraph.execute(node, state(check_plan={"sources": ["sales.customers"]}), rt)
    assert failed.value.type == "input_error"
    assert calls.count("describe_table") == 1


async def test_ingest_needs_the_workspace_and_tables(tmp_path):
    ws = fake_databricks.FakeWorkspace(tmp_path)
    st = state(check_plan={"sources": ["sales.orders"]})
    for target, problem in [
        (None, "平台没有配置 Databricks 工作区（AEP_DATABRICKS_HOST、AEP_DATABRICKS_TOKEN）"),
        (ws.target(), "平台没有配置 AEP_DATABRICKS_CLUSTER_ID"),
        (
            ws.target(databricks_cluster_id="x", databricks_source_catalog=""),
            "平台没有配置 AEP_DATABRICKS_SOURCE_CATALOG",
        ),
    ]:
        rt, ds = await node_env(target)
        with pytest.raises(NodeFailure) as failed:
            await run(ingest_node(ds), st, rt)
        assert (failed.value.type, failed.value.message) == ("databricks_error", problem)

    rt, ds = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))
    with pytest.raises(NodeFailure, match="没有指定要处理的表"):
        await run(ingest_node(ds), state(check_plan={"sources": []}), rt)
    ws.cluster_outputs = [{"errors": ["icsd-ace-dev.sales.orders: AnalysisException: 没有权限"]}]
    with pytest.raises(NodeFailure) as failed:
        await run(ingest_node(ds), st, rt)
    assert failed.value.type == "databricks_error"
    assert (
        "建表失败：icsd-ace-dev.sales.orders: AnalysisException: 没有权限" in failed.value.message
    )


@pytest.mark.parametrize(
    ("source", "spark"),
    [
        ("bigint", "BIGINT"),
        ("bigint unsigned", "DECIMAL(20,0)"),
        ("int(11)", "INT"),
        ("int(10) unsigned", "BIGINT"),
        ("int unsigned", "BIGINT"),
        ("tinyint(1)", "INT"),
        ("smallint unsigned", "INT"),
        ("integer", "INT"),
        ("decimal(12,2)", "DECIMAL(12,2)"),
        ("numeric(10)", "DECIMAL(10,0)"),
        ("NUMERIC", "DECIMAL(38,10)"),
        ("double precision", "DOUBLE"),
        ("real", "DOUBLE"),
        ("boolean", "BOOLEAN"),
        ("date", "DATE"),
        ("datetime", "TIMESTAMP"),
        ("timestamp with time zone", "TIMESTAMP"),
        ("varchar(64)", "STRING"),
        ("json", "STRING"),
        ("time", "STRING"),
        ("integer[]", "STRING"),
        ("numeric(12,2)[]", "STRING"),
        ("boolean[]", "STRING"),
    ],
)
def test_source_types_map_to_spark(source, spark):
    assert spark_type(source) == spark


def test_full_names_quote_only_what_needs_it():
    assert full_name("icsd-ace-dev", "retail", "orders") == "`icsd-ace-dev`.retail.orders"
    assert full_name("main", "a`b", "t") == "main.`a``b`.t"


# etl_code_check


def code_node(**changes) -> dict:
    return {
        "id": "check_code",
        "type": "etl_code_check",
        "title": "检查并打包",
        "reply": "{{codegen.text}}",
        "plan": "{{check_plan.plan}}",
        "source_name": "{{profile.source_name}}",
        "name": "{{start.name}}",
        "max_attempts": 3,
    } | changes


def code_state(text: str, name: str = "", previous: dict | None = None) -> dict:
    st = state(
        codegen={"text": text},
        check_plan={"plan": plan()},
        profile={"source_name": "示例库"},
        start={"name": name},
    )
    if previous:
        st["vars"]["check_code"] = previous
        st["private"] = {"check_code": {"failures": previous["failures"]}}
    return st


async def test_code_check_packages_code_that_passes(tmp_path):
    ws = fake_databricks.FakeWorkspace(tmp_path)
    rt, _ = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))
    good = reply({"targets": {"order_facts": PYSPARK_BODY}})
    out = await run(code_node(), code_state(good), rt)
    assert out.branch == "ok" and out.private == {"failures": 0}
    bundle = out.outputs["bundle"]
    assert bundle["name"] == "order_facts" and bundle["language"] == "pyspark"
    # Job mode (a cluster is set): the bundle runs as a notebook job.
    assert {"databricks.yml", codegen.PIPELINE_PY, codegen.JOB_NOTEBOOK} <= set(bundle["files"])
    assert "示例库" in bundle["files"]["databricks.yml"]
    assert out.outputs["code"] == {"order_facts": PYSPARK_BODY}
    assert out.outputs["summary"].startswith("已生成 Databricks bundle order_facts：README.md、")
    assert out.messages[0]["content"] == (
        f"代码检查通过，打包成 bundle order_facts（{len(bundle['files'])} 个文件）"
    )

    named = await run(code_node(), code_state(good, "retail_sales"), rt)
    assert named.outputs["bundle"]["name"] == "retail_sales"
    odd = await run(code_node(), code_state(good, "零售 销售"), rt)
    assert odd.outputs["bundle"]["name"] == "order_facts"

    # Without a workspace the bundle is a Lakeflow pipeline.
    rt, _ = await node_env()
    files = (await run(code_node(), code_state(good), rt)).outputs["bundle"]["files"]
    assert codegen.JOB_NOTEBOOK not in files


async def test_code_check_sends_problems_back():
    rt, _ = await node_env()
    bad = {"order_facts": 'return spark.sql("SELECT 1")'}
    first = await run(code_node(), code_state(reply({"targets": bad})), rt)
    assert first.branch == "retry" and first.private == {"failures": 1}
    assert first.outputs["bundle"] is None and first.outputs["code"] == bad
    assert first.outputs["problems"] == "order_facts: 不允许使用 .sql"
    assert first.outputs["summary"].startswith("代码没有通过检查，带着问题让模型重写")
    assert first.messages[0]["content"] == "代码有 1 处没通过检查，让模型重写（第 2/3 次）"

    previous = first.outputs | first.private
    second = await run(code_node(), code_state("不是 JSON", previous=previous), rt)
    assert second.outputs["code"] == bad
    assert second.outputs["problems"].startswith("回复不是要求的 JSON")
    assert second.messages[0]["content"] == "模型没按要求的 JSON 格式回答，让模型重写（第 3/3 次）"
    with pytest.raises(NodeFailure, match="代码连续 3 次没有通过检查"):
        await run(code_node(), code_state("{}", previous=second.outputs | second.private), rt)


async def test_code_check_needs_a_checked_plan():
    rt, _ = await node_env()
    st = code_state(reply({"targets": {}}))
    st["vars"]["check_plan"]["plan"] = None
    with pytest.raises(NodeFailure, match="方案要引用校验方案节点通过时的 plan"):
        await run(code_node(), st, rt)


# databricks_dev_run


def dev_node(**changes) -> dict:
    return {
        "id": "dev_run",
        "type": "databricks_dev_run",
        "title": "dev 运行",
        "bundle": "{{check_code.bundle}}",
    } | changes


def job_bundle() -> dict:
    built = codegen.render(
        "order_facts",
        EtlPlan.model_validate(plan()),
        {"order_facts": PYSPARK_BODY},
        "示例库",
        job=True,
    )
    return {"name": built.name, "language": "pyspark", "files": built.files}


def dev_state(visits: int = 0) -> dict:
    return state(check_code={"bundle": job_bundle()}) | {"visits": {"dev_run": visits}}


PASSED = {
    "tables": [
        {
            "name": "order_facts",
            "status": "COMPLETED",
            "rows": 2,
            "dropped": 1,
            "expectations": [{"name": "positive_quantity", "passed": 2, "failed": 1}],
        }
    ]
}
PREVIEW = {
    "tables": [
        {
            "target": [CATALOG, "trac_b_test", "order_facts"],
            "columns": ["order_id", "city", "quantity"],
            "rows": [{"order_id": 10, "city": "上海", "quantity": 2}],
        }
    ]
}


async def test_dev_run_passes_and_previews_the_result_tables(tmp_path):
    ws = fake_databricks.FakeWorkspace(tmp_path)
    ws.job_outputs = [PASSED]
    ws.cluster_outputs = [PREVIEW]
    rt, _ = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))
    out = await run(dev_node(preview_rows=5), dev_state(), rt)
    assert out.branch == "passed"
    o = out.outputs
    assert o["status"] == "passed"
    assert o["url"].startswith(fake_databricks.HOST)
    target = "`icsd-ace-dev`.trac_b_test.order_facts"
    assert o["tables"] == [
        {
            "name": "order_facts",
            "target": target,
            "rows": 2,
            "dropped": 1,
            "expectations": [{"name": "positive_quantity", "passed": 2, "failed": 1}],
        }
    ]
    assert o["preview"] == [
        {
            "table": "order_facts",
            "target": target,
            "columns": ["order_id", "city", "quantity"],
            "rows": [{"order_id": 10, "city": "上海", "quantity": 2}],
        }
    ]
    assert o["run"]["attempt"] == 1 and o["run"]["mode"] == "job" and "preview" not in o["run"]
    assert o["run"]["catalog"] == CATALOG and o["run"]["schema"] == "trac_b_test"
    assert o["errors"] == ""
    assert out.messages[0]["content"] == "dev 运行通过：写出 1 张表共 2 行，质量规则丢弃 1 行"
    for text in [
        "## dev 运行通过",
        f"- 运行方式：部署成 notebook Job，在已有集群 {fake_databricks.CLUSTER_ID} 上运行",
        "- 第 1 次运行",
        f"| `` {target} `` | 2 | 1 | positive_quantity 通过 2 / 不通过 1 |",
        f"### 数据预览：`` {target} ``（前 1 行）",
        "| order_id | city | quantity |",
        "| 10 | 上海 | 2 |",
    ]:
        assert text in o["report"]
    assert sent_spec(ws.commands[0]["command"], "preview") == {
        "tables": [[CATALOG, "trac_b_test", "order_facts"]],
        "rows": 5,
    }


async def test_a_later_run_of_the_bundle_waits_for_the_preview(tmp_path, monkeypatch):
    ws = fake_databricks.FakeWorkspace(tmp_path)
    ws.job_outputs = [PASSED, PASSED]
    ws.cluster_outputs = [PREVIEW, PREVIEW]
    target = ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID)
    steps: list[str] = []
    run_job, preview = target._run_job, target.preview

    async def logged_run(bundle):
        steps.append("run")
        return await run_job(bundle)

    async def logged_preview(tables, rows):
        steps.append("preview")
        await asyncio.sleep(0)  # the other run would start here without the lock
        found = await preview(tables, rows)
        steps.append("previewed")
        return found

    monkeypatch.setattr(target, "_run_job", logged_run)
    monkeypatch.setattr(target, "preview", logged_preview)
    built = codegen.render(
        "order_facts",
        EtlPlan.model_validate(plan()),
        {"order_facts": PYSPARK_BODY},
        "示例库",
        job=True,
    )
    first, second = await asyncio.gather(target.run(built, 5), target.run(built, 5))
    assert first.status == second.status == "passed"
    assert steps == ["run", "preview", "previewed"] * 2


async def test_dev_run_sends_fixable_errors_back_until_its_last_run(tmp_path):
    ws = fake_databricks.FakeWorkspace(tmp_path)
    error = "order_facts: AnalysisException: " + "列不存在" * 600
    ws.job_outputs = [{"errors": [error]}] * 3 + ["集群启动失败"]
    rt, _ = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))

    first = await run(dev_node(), dev_state(), rt)
    assert first.branch == "retry"
    assert first.outputs["status"] == "failed"
    assert first.outputs["errors"] == error[: data_nodes.ERROR_CHARS]
    assert "## dev 运行失败，带着报错回去重写代码" in first.outputs["report"]
    assert first.outputs["preview"] == [] and ws.commands == []
    assert first.messages[0]["content"] == "dev 运行失败（1 条报错），让模型改代码（第 2/3 次运行）"

    last = await run(dev_node(), dev_state(visits=2), rt)
    assert last.branch == "failed" and last.outputs["run"]["attempt"] == 3
    once = await run(dev_node(max_runs=1), dev_state(), rt)
    assert once.branch == "failed"
    # A failure that is not the code's (the job never reported) is not retried.
    broken = await run(dev_node(), dev_state(), rt)
    assert broken.branch == "failed" and broken.outputs["errors"] == "集群启动失败"
    for out in (last, once, broken):
        assert out.messages[0]["content"] == "dev 运行失败（1 条报错），不再重试"


def test_dev_run_says_how_it_went_in_one_sentence():
    cluster = DevRun("passed", "run", [], mode="cluster", cluster_id="c-1")
    unknown = {"rows": None, "dropped": None}
    counted = {"rows": 96, "dropped": 0}
    for dev, branch, tables, errors, expected in [
        (cluster, "passed", [], [], "dev 运行通过"),
        (cluster, "passed", [counted, unknown], [], "dev 运行通过：得到 2 张表"),
        (cluster, "passed", [counted], [], "dev 运行通过：得到 1 张表共 96 行"),
        (DevRun("failed", "deploy", []), "failed", [], [], "dev 运行失败，不再重试"),
    ]:
        assert data_nodes.dev_said(dev, 1, branch, tables, errors, 3) == expected


async def test_dev_run_needs_a_bundle_and_a_workspace(tmp_path):
    rt, _ = await node_env()
    with pytest.raises(NodeFailure) as failed:
        await run(dev_node(), dev_state(), rt)
    assert failed.value.type == "databricks_error"
    ws = fake_databricks.FakeWorkspace(tmp_path)
    rt, _ = await node_env(ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID))
    with pytest.raises(NodeFailure, match="bundle 要引用检查并打包节点的 bundle"):
        await run(dev_node(), state(check_code={"bundle": None}), rt)


def test_dev_report_shows_at_most_ten_columns():
    columns = [f"c{i}" for i in range(12)]
    dev = DevRun("passed", "run", [], mode="cluster", cluster_id="c-1")
    dev.preview = [{"table": "t", "target": "t", "columns": columns, "rows": [{"c0": 1}]}]
    report = data_nodes.dev_report(dev, 1, "passed", [], [])
    assert "只在集群 c-1 上执行转换逻辑，不部署、不写表" in report
    assert "| c0 | c1 | c2 | c3 | c4 | c5 | c6 | c7 | c8 | c9 |" in report
    assert "| 1 |   |" in report
    assert "共 12 列，只显示前 10 列。" in report


# A lakehouse ETL flow, as the etl-lakehouse template builds it


POLICY = "有效订单是支付成功且没有取消或退款的订单，状态取值 paid shipped completed。"


def lakehouse(ds: str, kb_id: str) -> dict:
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "inputs": [
                {"key": "datasource", "label": "数据源", "type": "datasource", "default": ds},
                {"key": "request", "label": "需求", "type": "textarea", "required": True},
                {"key": "tables", "label": "表范围", "type": "text", "default": "sales.*"},
                {
                    "key": "language",
                    "label": "代码语言",
                    "type": "select",
                    "default": "pyspark",
                    "choices": [{"value": "pyspark"}, {"value": "sql"}],
                },
                {"key": "name", "label": "bundle 名", "type": "text"},
            ],
        },
        profile_node(note="先看真实数据。"),
        {
            "id": "glossary",
            "type": "knowledge",
            "title": "查业务口径",
            "knowledge_bases": [kb_id],
            "query": "{{start.request}}",
            "top_k": 6,
        },
        {
            "id": "plan",
            "type": "llm",
            "title": "出方案",
            "skills": [{"name": "etl-plan", "version": 1}],
            "system": "按格式输出方案",
            "quiet": True,
            "max_visits": 10,
            "prompt": "需求：{{start.request}}\n源表：\n{{profile.description}}\n"
            "口径：{{glossary.context}}\n上一版方案：{{check_plan.plan}}\n"
            "问题：{{check_plan.problems}}\n意见：{{review.comment}}",
        },
        check_node(max_visits=10),
        {
            "id": "review",
            "type": "human",
            "title": "确认方案",
            "prompt": "确认这个方案吗？",
            "show": "{{check_plan.message}}",
            "options": [{"key": "approve", "label": "确认"}, {"key": "reject", "label": "驳回"}],
            "comment_required": ["reject"],
            "max_visits": 5,
        },
        ingest_node("{{start.datasource}}"),
        {
            "id": "codegen",
            "type": "llm",
            "title": "写代码",
            "skills": [{"name": "etl-codegen", "version": 1}],
            "quiet": True,
            "max_visits": 10,
            "prompt": "方案：{{check_plan.plan}}\n上一版代码：{{check_code.code}}\n"
            "问题：{{check_code.problems}}\n报错：{{dev_run.errors}}",
        },
        code_node(max_visits=10),
        dev_node(max_visits=3),
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [
                {"key": "report", "from": ["{{dev_run.report}}"]},
                {"key": "plan", "from": ["{{check_plan.plan}}"]},
                {"key": "ingested", "from": ["{{ingest.summary}}"]},
            ],
        },
    ]
    edges = [
        ("start", None, "profile"),
        ("profile", None, "glossary"),
        ("glossary", None, "plan"),
        ("plan", None, "check_plan"),
        ("check_plan", "ok", "review"),
        ("check_plan", "retry", "plan"),
        ("review", "approve", "ingest"),
        ("review", "reject", "plan"),
        ("ingest", None, "codegen"),
        ("codegen", None, "check_code"),
        ("check_code", "ok", "dev_run"),
        ("check_code", "retry", "codegen"),
        ("dev_run", "passed", "end"),
        ("dev_run", "retry", "codegen"),
        ("dev_run", "failed", "end"),
    ]
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": nodes,
            "edges": [
                {"from": a, "to": b} | ({"branch": branch} if branch else {})
                for a, branch, b in edges
            ],
        },
    }


class Lakehouse:
    def __init__(self, tmp_path, replies: list[str]) -> None:
        self.ws = fake_databricks.FakeWorkspace(tmp_path)
        self.settings = self.ws.settings(
            databricks_cluster_id=fake_databricks.CLUSTER_ID, knowledge_dir=tmp_path
        )
        self.gateway = FakeGateway(replies)
        self.knowledge = MemoryKnowledgeStore()
        self.sources = MemoryDataSourceStore()
        server = fake_connector.build()
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            default_apps(),
            AppContext(
                self.gateway.client,
                self.settings,
                datasources=self.sources,
                tools=McpTools(5, resolve=lambda url: server),
                databricks=self.ws.target(databricks_cluster_id=fake_databricks.CLUSTER_ID),
                knowledge=self.knowledge,
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
        self.app.dependency_overrides[get_settings] = lambda: self.settings

    async def setup(self, api: httpx.AsyncClient) -> None:
        self.api = api
        self.ds = (
            await self.sources.create("示例库", "postgres", fake_connector.URL)
        ).datasource_id
        made = await api.post("/knowledge-bases", json={"name": "零售数据口径"})
        self.kb_id = made.json()["kb_id"]
        uploaded = await api.post(
            f"/knowledge-bases/{self.kb_id}/documents",
            files={"files": ("01-有效订单定义.md", ("# 有效订单\n\n" + POLICY).encode())},
        )
        assert uploaded.status_code == 202, uploaded.text
        assert await self.worker.process_once()
        created = await api.post("/apps", json={"app": "retail-etl", "title": "零售数据 ETL"})
        assert created.status_code == 201, created.text
        config = lakehouse(self.ds, self.kb_id)
        saved = await api.put("/apps/retail-etl/draft", json={"config": config})
        assert saved.status_code == 200, saved.text
        published = await api.post("/apps/retail-etl/draft/publish", json={"note": "演示"})
        assert published.status_code == 200, published.text

    async def view(self, run_id: str) -> dict:
        await self.service.wait_idle()
        return (await self.api.get(f"/runs/{run_id}")).json()

    async def decide(self, run_id: str, decision: str, comment: str | None = None) -> dict:
        body = {"decision": decision, "comment": comment}
        resp = await self.api.post(f"/runs/{run_id}/resume", json=body)
        assert resp.status_code == 202, resp.text
        return await self.view(run_id)


@pytest.fixture
async def lake(tmp_path):
    missing = plan()
    missing["targets"][0]["sources"] = ["sales.missing"]
    full = plan(summary="改成全量 [1]。")
    full["targets"][0]["load"] = {"mode": "full", "key": None}
    good_code = reply({"targets": {"order_facts": PYSPARK_BODY}})
    platform = Lakehouse(
        tmp_path,
        [
            reply(missing),  # the plan check sends it back
            reply(plan(summary="订单明细宽表 [1]。")),  # a person sends it back
            reply(full),
            reply({"targets": {"order_facts": 'return spark.sql("SELECT 1")'}}),  # check
            good_code,  # fails on dev with a fixable error
            good_code,
        ],
    )
    platform.ws.job_outputs = [
        {"errors": ["order_facts: AnalysisException: 列 qty 不存在"]},
        PASSED,
    ]
    platform.ws.cluster_outputs = [
        loaded(("customers", 3), ("orders", 3)),
        {
            "tables": [
                {
                    "target": [CATALOG, "trac_b_test", "order_facts"],
                    "columns": ["order_id", "city", "quantity"],
                    "rows": [{"order_id": 10, "city": "上海", "quantity": 2}],
                }
            ]
        },
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=platform.app), base_url="http://test"
    ) as api:
        await platform.setup(api)
        yield platform
    await platform.service.wait_idle()


async def test_lakehouse_flow_from_request_to_dev_run(lake):
    made = await lake.api.post(
        "/runs",
        json={
            "app": "retail-etl",
            "input": {"request": POLICY, "name": "order_facts"},
            "requester": "李雷",
        },
    )
    assert made.status_code == 201, made.text
    run_id = made.json()["run_id"]
    view = await lake.view(run_id)
    assert view["status"] == "waiting", view["error"]
    shown = view["pending"]["details"]["show"]
    assert "**方案**：订单明细宽表 [1]。" in shown and "依据的资料" in shown
    # The plan check's problems went back to the model.
    second_plan = lake.gateway.requests[-1]["body"]["messages"][-1]["content"]
    assert "引用了不存在的源表 'sales.missing'" in second_plan

    view = await lake.decide(run_id, "reject", "先做全量")
    assert view["status"] == "waiting"
    third_plan = lake.gateway.requests[-1]["body"]["messages"][-1]["content"]
    assert "意见：先做全量" in third_plan and "订单明细宽表" in third_plan

    view = await lake.decide(run_id, "approve")
    assert view["status"] == "done", view["error"]
    result = view["result"]
    assert result["plan"]["summary"] == "改成全量 [1]。"
    assert result["plan"]["references"][0]["document_name"] == "01-有效订单定义.md"
    assert result["ingested"].startswith("已把 2 张表共 6 行抽取到 Databricks")
    assert "## dev 运行通过" in result["report"] and "- 第 2 次运行" in result["report"]
    assert "| 10 | 上海 | 2 |" in result["report"]
    # Each step that has something to say says it in a sentence; the model's replies
    # (plan, codegen) are quiet: only their checks speak.
    messages = view["messages"]
    assert [m["node"] for m in messages] == [
        "start", "profile", "check_plan", "check_plan", "review", "check_plan", "review",
        "ingest", "check_code", "check_code", "dev_run", "check_code", "dev_run",
    ]  # fmt: skip
    assert (messages[0]["actor"], messages[0]["content"]) == ("李雷", POLICY)
    assert [m["content"] for m in messages if m["node"] == "review"] == [
        "选了「驳回」，意见：先做全量",
        "选了「确认」",
    ]
    assert messages[-1]["content"] == "dev 运行通过：写出 1 张表共 2 行，质量规则丢弃 1 行"
    # The approved plan cites [1] in its references.
    assert {s["index"] for s in view["sources"] if s["cited"]} == {1}
    # Only the plan's sources were copied, after the person approved.
    spec = sent_spec(lake.ws.commands[0]["command"], "ingest")
    assert [t["target"][-1] for t in spec["tables"]] == ["customers", "orders"]
    assert spec["tables"][0]["properties"]["aep.run_id"] == run_id
    # The static check's problems, then the dev run's errors, went back to the model.
    prompts = [r["body"]["messages"][-1]["content"] for r in lake.gateway.requests[-3:]]
    assert "报错：" in prompts[0] and "spark.sql" not in prompts[0]
    assert "spark.sql" in prompts[1]
    assert "列 qty 不存在" in prompts[2]

    flow = (await lake.api.get(f"/runs/{run_id}/flow")).json()
    ran = [s["node"] for s in flow["steps"]]
    assert ran == [
        "start", "profile", "glossary",
        "plan", "check_plan", "plan", "check_plan", "review",
        "plan", "check_plan", "review",
        "ingest", "codegen", "check_code", "codegen", "check_code", "dev_run",
        "codegen", "check_code", "dev_run", "end",
    ]  # fmt: skip
    nodes = {n["id"]: n for n in flow["nodes"]}
    assert nodes["profile"]["design"]["note"] == "先看真实数据。"
    assert nodes["glossary"]["design"]["note"] is None
    assert nodes["ingest"]["kind"] == "tool" and nodes["check_plan"]["kind"] == "code"
    labels = {(e["source"], e["target"]): e["label"] for e in flow["edges"]}
    assert labels[("dev_run", "codegen")] == "带着报错重写"
    assert labels[("check_plan", "plan")] == "重写"


# Saving: the loop rule, notes, references from data nodes


async def test_a_loop_may_end_at_a_check_node(lake):
    config = lakehouse(lake.ds, lake.kb_id)
    saved = await lake.api.put("/apps/retail-etl/draft", json={"config": config})
    assert saved.status_code == 200, saved.text

    nodes = config["graph"]["nodes"]
    nodes.append({"id": "again", "type": "template", "title": "再来", "template": "x"})
    edges = config["graph"]["edges"]
    edges.remove({"from": "ingest", "to": "codegen"})
    edges += [{"from": "ingest", "to": "again"}, {"from": "again", "to": "ingest"}]
    saved = await lake.api.put("/apps/retail-etl/draft", json={"config": config})
    assert saved.status_code == 422
    problems = {tuple(p["loc"][2:]): p["msg"] for p in saved.json()["detail"]}
    assert problems[("graph", "nodes", "ingest")] == (
        "这个循环里没有能决定离开循环的节点（人工确认、条件判断、意图分类或带分支的"
        "校验节点），会一直循环下去"
    )


async def test_notes_are_checked_and_kept_verbatim(lake):
    config = lakehouse(lake.ds, lake.kb_id)
    nodes = {n["id"]: n for n in config["graph"]["nodes"]}
    nodes["plan"]["note"] = "这里写 {{nope.text}} 也只是文字"
    saved = await lake.api.put("/apps/retail-etl/draft", json={"config": config})
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft"]["config"]["graph"]["nodes"][3]["note"].startswith("这里写")

    nodes["plan"]["note"] = "长" * 501
    nodes["profile"]["note"] = 3
    saved = await lake.api.put("/apps/retail-etl/draft", json={"config": config})
    problems = {tuple(p["loc"][2:]): p["msg"] for p in saved.json()["detail"]}
    assert problems[("graph", "nodes", "plan", "note")] == "讲解说明最多 500 个字符"
    assert problems[("graph", "nodes", "profile", "note")] == "讲解说明必须是文字"


async def test_data_node_settings_are_checked(lake):
    config = lakehouse(lake.ds, lake.kb_id)
    nodes = {n["id"]: n for n in config["graph"]["nodes"]}
    nodes["profile"] |= {"tables": "orders", "sample_rows": 50}
    nodes["check_plan"] |= {"profile": "探查结果", "hits": ["x"]}
    nodes["ingest"] |= {"tables": "", "max_rows": 0}
    nodes["check_code"]["plan"] = "{{check_plan.plan}} 和别的"
    nodes["dev_run"] |= {"max_runs": 4, "preview_rows": 101}
    saved = await lake.api.put("/apps/retail-etl/draft", json={"config": config})
    assert saved.status_code == 422
    problems = {tuple(p["loc"][4:]): p["msg"] for p in saved.json()["detail"]}
    assert problems == {
        ("profile", "tables"): "orders 要写成 schema.table 或 schema.*",
        ("profile", "sample_rows"): "样本行数要在 1–20 之间",
        ("check_plan", "profile"): "探查结果必须恰好是一个变量，如 {{profile.tables}}",
        ("check_plan", "hits", 0): "每项必须恰好是一个变量，如 {{glossary.hits}}",
        ("ingest", "tables"): "要指定表",
        ("ingest", "max_rows"): "每张表最多行数要在 1–1000000 之间",
        ("check_code", "plan"): "方案必须恰好是一个变量，如 {{check_plan.plan}}",
        ("dev_run", "max_runs"): "最多运行次数要在 1–3 之间",
        ("dev_run", "preview_rows"): "预览行数要在 0–100 之间",
    }


async def test_nodes_that_write_databricks_cannot_be_tried_alone(lake):
    for node in ("ingest", "dev_run"):
        resp = await lake.api.post(f"/apps/retail-etl/nodes/{node}/test", json={"values": {}})
        assert resp.status_code == 422
        assert "会写 Databricks，不能单独试跑" in resp.json()["detail"]
    tried = await lake.api.post(
        "/apps/retail-etl/nodes/profile/test",
        json={"values": {"start.datasource": lake.ds, "start.tables": "sales.orders"}},
    )
    assert tried.json()["status"] == "done", tried.text
    assert tried.json()["outputs"]["names"] == ["sales.orders"]


async def test_node_types_list_the_data_group():
    types = {t["type"]: t for t in (TYPES[k].describe() for k in TYPES)}
    data = [k for k, t in types.items() if t["category"] == "data"]
    assert data == [
        "etl_profile",
        "etl_plan_check",
        "databricks_ingest",
        "etl_code_check",
        "databricks_dev_run",
    ]
    assert all(types[k]["error_handling"] for k in data)
    assert types["databricks_dev_run"]["defaults"] == {
        "bundle": "",
        "max_runs": 3,
        "preview_rows": 20,
    }


def test_exports_name_the_data_sources_of_data_nodes():
    config = lakehouse("ds_1", "kb_1")
    assert files.datasource_ids(config) == []
    config["graph"]["nodes"][6]["datasource"] = "ds_2"
    assert files.datasource_ids(config) == ["ds_2"]
