"""The ETL demo (docs/api/data-nodes.md, "演示"): the etl-lakehouse template, the two
knowledge bases' documents against the sample data and the code checks, GET
/health/demo, and aep-demo making everything on a platform and changing nothing the
second time."""

import re
import shutil
from pathlib import Path

import httpx
import pytest

from aep import demo
from aep.databricks import DatabricksError
from aep.etl import codegen
from aep.etl import plan as etl_plan
from aep.etl.plan import EtlPlan
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import builtin
from aep.tools.mcp import McpTools
from tests import fake_connector
from tests.test_etl import PYSPARK_BODY, plan, reply
from tests.test_flow_data_nodes import PASSED, PREVIEW, Lakehouse, loaded

ROOT = Path(__file__).resolve().parents[2]
SEED = (ROOT / "connectors/mysql/seed/003-retail.sql").read_text()
CONTRACT = (ROOT / "docs/api/data-nodes.md").read_text()
KNOWLEDGE = ROOT / "demo/knowledge"


def docs(folder: str) -> dict[str, str]:
    return {k: v.decode() for k, v in demo.files_of(KNOWLEDGE / folder).items()}


# The knowledge bases' documents


def test_documents_are_the_ones_the_contract_names():
    assert list(docs("retail-metrics")) == [
        "01-有效订单定义.md", "02-销售额与客单价.md", "03-城市与会员等级.md",
        "04-时间口径.md", "05-客户数与复购.md", "06-毛利.md",
    ]  # fmt: skip
    assert list(docs("dw-standards")) == [
        "01-分层与命名.md", "02-主键与字段.md", "03-数据质量规则.md",
        "04-加载策略.md", "05-PySpark编码规范.md",
    ]  # fmt: skip
    for folder in ("retail-metrics", "dw-standards"):
        assert (KNOWLEDGE / folder / "README.md").is_file()
        for name, text in docs(folder).items():
            assert text.startswith("# "), name
            for field in ("适用范围", "版本", "维护部门", "要点"):
                assert f"- {field}：" in text, (name, field)


def seed_columns() -> dict[str, set[str]]:
    tables = {}
    for name, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\)", SEED, re.S):
        tables[name] = {line.split()[0] for line in body.strip().splitlines()} - {
            "PRIMARY",
            "FOREIGN",
        }
    return tables


def test_metric_definitions_match_the_sample_data():
    text = "\n".join(docs("retail-metrics").values())
    # Every value a definition relies on is in the data, spelled the same.
    values = [
        "paid", "shipped", "completed", "cancelled", "refunded", "测试",
        "normal", "silver", "gold", "online", "store", "mini_program",
        "上海", "北京", "杭州", "南京", "苏州", "深圳", "广州", "成都",
        "家电", "数码", "个护", "食品", "家居",
    ]  # fmt: skip
    for value in values:
        assert value in text, value
        assert f"'{value}" in SEED, value
    # The dirty spellings the definitions clean up are there too.
    for dirty in ("' Paid '", "'COMPLETED'", "'上海市'", "' 上海 '", "'Gold'", "'SILVER'"):
        assert dirty in SEED
    # Every table.column the documents name exists.
    columns = seed_columns()
    named = re.findall(r"`(customers|products|orders|order_items)\.(\w+)`", text)
    assert len(named) > 10
    for table, column in named:
        assert column in columns[table], f"{table}.{column}"


def test_pyspark_standards_pass_the_platforms_code_check():
    text = docs("dw-standards")["05-PySpark编码规范.md"]
    for name in sorted(codegen.FORBIDDEN_ATTRS | codegen.FORBIDDEN_NAMES):
        assert re.search(rf"[`.]{re.escape(name)}\b", text), name
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    assert len(blocks) == 2
    sources = ["retail.orders", "retail.order_items", "retail.customers"]
    standards_plan = EtlPlan.model_validate(
        {
            "summary": "规范里的例子",
            "targets": [
                {
                    "name": "dwd_retail_order_detail",
                    "sources": sources,
                    "logic": "x",
                    "columns": [{"name": "order_id", "source": "order_id"}],
                },
                {
                    "name": "dws_retail_sales_city_month",
                    "sources": ["dwd_retail_order_detail"],
                    "logic": "x",
                    "columns": [{"name": "stat_month", "source": "stat_month"}],
                },
            ],
        }
    )
    code = dict(zip([t.name for t in standards_plan.targets], blocks, strict=True))
    assert codegen.check_code(standards_plan, code) == []
    # The naming rule's examples are names a plan may use.
    for table in re.findall(r"`((?:dwd|dws)_\w+)`", "\n".join(docs("dw-standards").values())):
        assert etl_plan.IDENTIFIER.match(table) and table not in etl_plan.RESERVED, table


# The template


def contract_block(after: str) -> str:
    start = CONTRACT.index(after)
    return re.search(r"```\n(.*?)\n```", CONTRACT[start:], re.S)[1]  # type: ignore[index]


def test_template_follows_the_contract():
    template = builtin.TEMPLATES["etl-lakehouse"]
    assert (template.title, template.needs) == (
        "数据 ETL（湖仓）",
        ("datasource", "knowledge_base"),
    )
    nodes = {n["id"]: n for n in template.config["graph"]["nodes"]}
    assert [(i, n["type"], n["title"]) for i, n in nodes.items()] == [
        ("start", "start", "开始"),
        ("profile", "etl_profile", "探查数据"),
        ("glossary", "knowledge", "查业务口径"),
        ("standards", "knowledge", "查开发规范"),
        ("plan", "llm", "出方案"),
        ("check_plan", "etl_plan_check", "校验方案"),
        ("review", "human", "确认方案"),
        ("ingest", "databricks_ingest", "抽取到湖仓"),
        ("codegen", "llm", "写代码"),
        ("check_code", "etl_code_check", "检查并打包"),
        ("dev_run", "databricks_dev_run", "dev 运行"),
        ("end", "end", "结束"),
    ]
    assert all(0 < len(n["note"]) <= 500 for n in nodes.values())
    retries = {i: n["on_error"]["retry"] for i, n in nodes.items() if "on_error" in n}
    assert retries == {
        "profile": {"max": 2, "interval_seconds": 5},
        "plan": {"max": 1, "interval_seconds": 2},
        "ingest": {"max": 2, "interval_seconds": 5},
        "codegen": {"max": 1, "interval_seconds": 2},
        "dev_run": {"max": 2, "interval_seconds": 5},
    }
    assert nodes["plan"]["prompt"] == contract_block("`plan` 的提示词")
    assert nodes["codegen"]["prompt"] == contract_block("`codegen` 的提示词")
    assert nodes["plan"]["system"] == etl_plan.OUTPUT_FORMAT
    assert nodes["codegen"]["system"] == codegen.OUTPUT_FORMAT
    # Their replies are JSON for the checks, kept out of the run's messages.
    assert {i for i, n in nodes.items() if "quiet" in n} == {"plan", "codegen"}
    assert nodes["plan"]["quiet"] is True and nodes["codegen"]["quiet"] is True
    assert (
        nodes["standards"]["query"]
        == "分层与命名、主键与字段类型、数据质量规则、加载策略、PySpark 编码规范"
    )
    assert (nodes["glossary"]["top_k"], nodes["standards"]["top_k"]) == (12, 20)
    assert nodes["check_plan"]["hits"] == ["{{glossary.hits}}", "{{standards.hits}}"]
    edges = {(e["from"], e.get("branch"), e["to"]) for e in template.config["graph"]["edges"]}
    assert edges == {
        ("start", None, "profile"), ("profile", None, "glossary"),
        ("glossary", None, "standards"), ("standards", None, "plan"),
        ("plan", None, "check_plan"),
        ("check_plan", "ok", "review"), ("check_plan", "retry", "plan"),
        ("review", "approve", "ingest"), ("review", "reject", "plan"),
        ("ingest", None, "codegen"), ("codegen", None, "check_code"),
        ("check_code", "ok", "dev_run"),
        ("check_code", "retry", "codegen"), ("dev_run", "passed", "end"),
        ("dev_run", "retry", "codegen"), ("dev_run", "failed", "end"),
    }  # fmt: skip


# aep-demo on a platform


class DemoPlatform(Lakehouse):
    """The lakehouse test platform with what GET /health/demo reads."""

    def __init__(self, tmp_path, replies: list[str]) -> None:
        super().__init__(tmp_path, replies)

        async def models() -> list[str]:
            return ["company", "company-embed", "mock"]

        self.catalog = AppCatalog(self.service, self.sources, self.settings, models)
        self.app.state.catalog = self.catalog
        server = fake_connector.build()
        self.app.state.mcp_tools = McpTools(5, resolve=lambda url: server)
        self.app.state.databricks = self.ws.target(
            databricks_cluster_id=self.settings.databricks_cluster_id
        )
        self.lines: list[str] = []

    async def setup(self, api: httpx.AsyncClient) -> None:
        self.api = api
        made = await self.sources.create(demo.DEMO_DATASOURCE, "mysql", fake_connector.URL)
        self.ds = made.datasource_id

    def demo(self, knowledge_dir: Path = KNOWLEDGE) -> demo.Demo:
        async def process(_: float) -> None:
            while await self.worker.process_once():
                pass

        self.lines = []
        return demo.Demo(
            self.api, knowledge_dir=knowledge_dir, out=self.lines.append, sleep=process
        )


@pytest.fixture
async def platform(tmp_path, monkeypatch):
    # The sample source has the demo's retail database too.
    for (_, table), spec in list(fake_connector.TABLES.items()):
        monkeypatch.setitem(fake_connector.TABLES, ("retail", table), spec)
    bad_plan = plan()
    bad_plan["targets"][0]["sources"] = ["sales.missing"]
    good_code = reply({"targets": {"order_facts": PYSPARK_BODY}})
    lake = DemoPlatform(
        tmp_path,
        [
            reply(bad_plan),  # the plan check sends it back
            reply(plan(summary="订单明细宽表 [1]。")),  # a person sends it back
            reply(plan(summary="改好了。")),
            reply({"targets": {"order_facts": 'return spark.sql("SELECT 1")'}}),
            good_code,  # fails on dev with a fixable error
            good_code,
        ],
    )
    lake.ws.job_outputs = [{"errors": ["order_facts: AnalysisException: 列 qty 不存在"]}, PASSED]
    lake.ws.cluster_outputs = [loaded(("customers", 3), ("orders", 3)), PREVIEW]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=lake.app), base_url="http://test"
    ) as api:
        await lake.setup(api)
        yield lake
    await lake.service.wait_idle()


async def test_health_demo_lists_what_the_demo_needs(platform):
    items = (await platform.api.get("/health/demo")).json()
    assert [(i["key"], i["ok"], i["required"]) for i in items] == [
        ("api", True, True),
        ("model", True, True),
        ("embedding", True, True),
        ("datasource", True, True),
        ("databricks", True, False),
        ("databricks_cluster", True, False),
        ("databricks_source_catalog", True, False),
        ("databricks_target", True, False),
    ]
    by_key = {i["key"]: i for i in items}
    assert by_key["model"]["value"] == "company"
    assert by_key["embedding"]["value"] == "company-embed"
    assert by_key["datasource"]["detail"] == "数据源「MySQL 示例库」能读到 retail 库"
    assert by_key["databricks_cluster"]["detail"].endswith("正在运行")
    assert "dapi-test-token" not in str(items)

    async def denied() -> str:
        raise DatabricksError("/api/2.0/clusters/get: HTTP 403 Invalid access token")

    platform.app.state.databricks.cluster_state = denied
    platform.settings.demo_model = "nope"
    platform.settings.demo_embedding_model = "other-embed"
    platform.settings.databricks_catalog = ""
    del fake_connector.TABLES[("retail", "orders")], fake_connector.TABLES[("retail", "customers")]
    items = {i["key"]: i for i in (await platform.api.get("/health/demo")).json()}
    assert (
        not items["model"]["ok"]
        and "没有 nope（现有：company、company-embed、mock）" in items["model"]["detail"]
    )
    assert (
        not items["embedding"]["ok"]
        and "不在 AEP_EMBEDDING_MODELS 里" in items["embedding"]["detail"]
    )
    platform.settings.embedding_models = ["mock-embed", "other-embed"]
    items = {i["key"]: i for i in (await platform.api.get("/health/demo")).json()}
    assert (
        not items["embedding"]["ok"]
        and "模型网关上没有向量模型 other-embed" in items["embedding"]["detail"]
    )
    platform.settings.demo_embedding_model = "mock-embed"
    items = {i["key"]: i for i in (await platform.api.get("/health/demo")).json()}
    assert items["embedding"]["ok"]
    assert not items["datasource"]["ok"] and "没有 retail 库" in items["datasource"]["detail"]
    assert (
        not items["databricks_cluster"]["ok"]
        and "令牌过期" in items["databricks_cluster"]["detail"]
    )
    assert not items["databricks_target"]["ok"]

    platform.settings.databricks_token = ""
    items = (await platform.api.get("/health/demo")).json()
    assert items[-1]["key"] == "databricks" and not items[-1]["ok"]


async def test_demo_makes_everything_once_and_the_app_runs_to_the_end(platform, tmp_path):
    assert await platform.demo().run(), platform.lines
    assert platform.lines[:6] == [
        "1. 检查",
        "  ✓ 平台接口正常",
        "  ✓ 模型网关上有 company",
        "  ✓ 知识库用向量模型 company-embed",
        "  ✓ 数据源「MySQL 示例库」能读到 retail 库",
        "  ✓ 工作区 adb-1.test.databricks.azure.cn",
    ]
    assert "  ✓ 零售数据口径：新建、上传 6 篇（6 篇文档，向量模型 company-embed）" in platform.lines
    assert "  ✓ 数仓开发规范：新建、上传 5 篇（5 篇文档，向量模型 company-embed）" in platform.lines
    assert "  ✓ 11 篇文档都已处理完成，可以检索" in platform.lines
    assert "  ✓ 用模板 etl-lakehouse 新建应用「零售数据 ETL」（retail-etl）" in platform.lines
    assert "  ✓ 应用「零售数据 ETL」发布了版本 2" in platform.lines
    assert platform.lines[-5] == "  应用：http://localhost:3000/apps/retail-etl"
    assert platform.lines[-1] == f"  备用需求：{demo.SPARE_REQUEST}"

    # The app as the demo needs it: bases, model and the start node's defaults.
    detail = (await platform.api.get("/apps/retail-etl")).json()
    assert detail["draft"] is None and detail["current"]["version"] == 2
    nodes = {n["id"]: n for n in detail["current"]["config"]["graph"]["nodes"]}
    bases = {b["name"]: b["kb_id"] for b in (await platform.api.get("/knowledge-bases")).json()}
    assert nodes["glossary"]["knowledge_bases"] == [bases["零售数据口径"]]
    assert nodes["standards"]["knowledge_bases"] == [bases["数仓开发规范"]]
    assert nodes["plan"]["model"] == nodes["codegen"]["model"] == "company"
    defaults = {f["key"]: f.get("default") for f in nodes["start"]["inputs"]}
    assert defaults == {
        "datasource": platform.ds,
        "request": demo.REQUEST,
        "tables": "retail.*",
        "language": "pyspark",
        "name": None,
    }
    manual = (await platform.api.get("/apps/retail-etl/manual")).json()
    assert [(b["name"], b["document_count"]) for b in manual["knowledge_bases"]] == [
        ("零售数据口径", 6),
        ("数仓开发规范", 5),
    ]
    assert manual["datasources"][0]["name"] == "运行时选择（开始节点的「数据源」）"

    # Again: nothing changes.
    assert await platform.demo().run(), platform.lines
    assert "  ✓ 零售数据口径：已是最新（6 篇文档，向量模型 company-embed）" in platform.lines
    assert "  ✓ 数仓开发规范：已是最新（5 篇文档，向量模型 company-embed）" in platform.lines
    assert "  ✓ 应用「零售数据 ETL」已是最新（版本 2）" in platform.lines
    assert len((await platform.api.get("/apps/retail-etl/versions")).json()) == 2

    # A changed file is replaced; a document without a file is left alone.
    edited = tmp_path / "knowledge"
    shutil.copytree(KNOWLEDGE, edited)
    (edited / "dw-standards/04-加载策略.md").write_text("# 加载策略\n\n全部全量重算。\n")
    kb = bases["零售数据口径"]
    extra = await platform.api.post(
        f"/knowledge-bases/{kb}/documents", files={"files": ("补充.md", b"# x\n\ny\n")}
    )
    assert extra.status_code == 202
    assert await platform.demo(edited).run(), platform.lines
    assert "  ✓ 数仓开发规范：更新 1 篇（5 篇文档，向量模型 company-embed）" in platform.lines
    assert "  ✓ 零售数据口径：已是最新（6 篇文档，向量模型 company-embed）" in platform.lines
    names = [d["name"] for d in (await platform.api.get(f"/knowledge-bases/{kb}/documents")).json()]
    assert "补充.md" in names

    # The demo app from the request to the dev run, with the start node's defaults.
    made = await platform.api.post(
        "/runs", json={"app": "retail-etl", "input": {"tables": "sales.*"}}
    )
    assert made.status_code == 201, made.text
    run_id = made.json()["run_id"]
    view = await platform.view(run_id)
    assert view["status"] == "waiting", view["error"]
    assert "**方案**：订单明细宽表" in view["pending"]["details"]["show"]
    view = await platform.decide(run_id, "reject", "先做全量")
    view = await platform.decide(run_id, "approve")
    assert view["status"] == "done", view["error"]
    result = view["result"]
    assert result["ingested"].startswith("已把 2 张表共 6 行抽取到 Databricks")
    assert "## dev 运行通过" in result["report"] and "| 10 | 上海 | 2 |" in result["report"]
    flow = (await platform.api.get(f"/runs/{run_id}/flow")).json()
    assert [s["node"] for s in flow["steps"]] == [
        "start", "profile", "glossary", "standards",
        "plan", "check_plan", "plan", "check_plan", "review",
        "plan", "check_plan", "review",
        "ingest", "codegen", "check_code", "codegen", "check_code", "dev_run",
        "codegen", "check_code", "dev_run", "end",
    ]  # fmt: skip
    prompts = [r["body"]["messages"] for r in platform.gateway.requests if "messages" in r["body"]]
    first_plan = prompts[0][-1]["content"]
    assert demo.REQUEST in first_plan and "期望的代码语言：pyspark" in first_plan
    assert prompts[0][0]["content"].endswith(etl_plan.OUTPUT_FORMAT)
    assert "开发规范（参考资料）" in prompts[-1][-1]["content"]


async def test_demo_switches_the_embedding_model_of_its_own_bases(platform):
    platform.settings.demo_embedding_model = "mock-embed"
    assert await platform.demo().run(), platform.lines
    assert "  ✓ 数仓开发规范：新建、上传 5 篇（5 篇文档，向量模型 mock-embed）" in platform.lines

    platform.settings.demo_embedding_model = "company-embed"
    assert await platform.demo().run(), platform.lines
    assert (
        "  ✓ 数仓开发规范：向量模型从 mock-embed 换成 company-embed、上传 5 篇"
        "（5 篇文档，向量模型 company-embed）" in platform.lines
    )
    bases = {b["name"]: b for b in (await platform.api.get("/knowledge-bases")).json()}
    assert {b["embedding_model"] for b in bases.values()} == {"company-embed"}
    kb = bases["零售数据口径"]["kb_id"]
    documents = (await platform.api.get(f"/knowledge-bases/{kb}/documents")).json()
    assert len(documents) == 6 and {d["status"] for d in documents} == {"ready"}

    # A document that is not the demo's is never deleted to switch.
    extra = await platform.api.post(
        f"/knowledge-bases/{kb}/documents", files={"files": ("补充.md", b"# x\n\ny\n")}
    )
    assert extra.status_code == 202
    platform.settings.demo_embedding_model = "mock-embed"
    assert not await platform.demo().run()
    assert "不是演示的文档：补充.md" in platform.lines[-1]
    assert "AEP_DEMO_EMBEDDING_MODEL=company-embed" in platform.lines[-1]
    documents = (await platform.api.get(f"/knowledge-bases/{kb}/documents")).json()
    assert len(documents) == 7
    base = (await platform.api.get(f"/knowledge-bases/{kb}")).json()
    assert base["embedding_model"] == "company-embed"


async def test_demo_stops_when_something_required_is_missing(platform):
    platform.settings.demo_model = "nope"
    assert not await platform.demo().run()
    assert platform.lines[-1] == "  ✗ 上面打 ✗ 的项是必需的，解决后再运行 make demo"
    assert (await platform.api.get("/knowledge-bases")).json() == []

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unreachable), base_url="http://localhost:8000"
    ) as api:
        lines: list[str] = []
        assert not await demo.Demo(api, out=lines.append).run()
    assert "连不上平台接口 http://localhost:8000" in lines[-1] and "make up" in lines[-1]
