"""The etl-lakehouse template, version 2 (docs/api/data-nodes.md, "应用模板 `etl-lakehouse`
第 2 版"), on the fake model, connector and Databricks workspace: confirmed twice and
delivered, the code or the plan sent back after the dev run, stopped, and a failed dev
run that cannot be passed."""

import copy

import httpx
import pytest

from aep.runtime.flow import builtin
from tests.test_etl import PYSPARK_BODY, plan, reply
from tests.test_flow_data_nodes import (
    PASSED,
    POLICY,
    PREVIEW,
    Lakehouse,
    brief_reply,
    loaded,
    sent_spec,
)

GOOD_CODE = reply({"targets": {"order_facts": PYSPARK_BODY}})
INGESTED = loaded(("customers", 3), ("orders", 3))
FIXABLE = {"errors": ["order_facts: AnalysisException: 列 qty 不存在"]}


def v2_plan(**changes) -> str:
    return reply(v2_value(**changes))


def v2_value(**changes) -> dict:
    """A version 2 plan with one target and two questions (q1, q2)."""
    detail = plan()["targets"][0]
    for column, label in zip(detail["columns"], ["订单号", "城市", "数量"], strict=True):
        column["label"] = label
    value = brief_reply(targets=[detail])
    value["brief"]["metrics"] = [
        {
            "id": "m1",
            "name": "商品数量",
            "definition": "有效订单的数量之和 [1]",
            "column": "quantity",
        }
    ]
    value.update(changes)
    return value


def template(kb_id: str) -> dict:
    config = copy.deepcopy(builtin.TEMPLATES["etl-lakehouse"].config)
    for node in config["graph"]["nodes"]:
        if node["type"] == "knowledge":
            node["knowledge_bases"] = [kb_id]
    return config


class Template(Lakehouse):
    async def publish(self, **ingest) -> None:
        config = template(self.kb_id)
        nodes = {n["id"]: n for n in config["graph"]["nodes"]}
        nodes["ingest"].update(ingest)
        saved = await self.api.put("/apps/retail-etl/draft", json={"config": config})
        assert saved.status_code == 200, saved.text
        published = await self.api.post("/apps/retail-etl/draft/publish", json={"note": "第 2 版"})
        assert published.status_code == 200, published.text

    async def start(self) -> tuple[str, dict]:
        made = await self.api.post(
            "/runs",
            json={
                "app": "retail-etl",
                "input": {"datasource": self.ds, "request": POLICY, "tables": "sales.*"},
                "requester": "李雷",
            },
        )
        assert made.status_code == 201, made.text
        run_id = made.json()["run_id"]
        view = await self.view(run_id)
        assert view["status"] == "waiting", view["error"]
        return run_id, view

    async def answer(self, run_id: str, decision: str, **body) -> httpx.Response:
        body = {"decision": decision, "actor": "韩梅梅"} | body
        return await self.api.post(f"/runs/{run_id}/resume", json=body)

    async def submit(self, run_id: str, decision: str, **body) -> dict:
        resp = await self.answer(run_id, decision, **body)
        assert resp.status_code == 202, resp.text
        return await self.view(run_id)

    def prompt(self, back: int = 1) -> str:
        return self.gateway.requests[-back]["body"]["messages"][-1]["content"]


SUGGESTED = {"q1": "all", "q2": "unknown"}


@pytest.fixture
async def make(tmp_path):
    made: list[Template] = []

    async def build(
        replies: list[str], jobs: list[dict], commands: list[dict], **ingest
    ) -> Template:
        platform = Template(tmp_path, replies)
        platform.ws.job_outputs = jobs
        platform.ws.cluster_outputs = commands
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=platform.app), base_url="http://test"
        )
        await platform.setup(client)
        await platform.publish(**ingest)
        made.append(platform)
        return platform

    yield build
    for platform in made:
        await platform.service.wait_idle()
        await platform.api.aclose()


def steps(view: dict) -> list[str]:
    return [m["node"] for m in view["messages"]]


async def test_confirmed_twice_then_delivered(make):
    lake = await make(
        [v2_plan(), v2_plan(summary="只看 2026 年。"), GOOD_CODE], [PASSED], [INGESTED, PREVIEW]
    )
    run_id, view = await lake.start()
    pending = view["pending"]
    assert view["step"]["node"] == "review" and pending["changed_branch"] == "revise"
    assert pending["options"] == ["approve", "revise"]
    assert [f["key"] for f in pending["fields"]] == ["q1", "q2"]
    details = pending["details"]
    assert details["view"] == "etl_plan_review" and details["round"] == 1
    assert details["data"]["brief"]["goal"] == "按城市统计有效订单的订单数和商品数量"
    assert details["data"]["returned"] is None and details["data"]["previous"] is None
    assert details["data"]["answer"]["target"] == "order_facts"
    assert "## 业务说明" in details["show"]

    # A changed answer turns 确认 into 按我的意见修改方案: the plan is written again.
    view = await lake.submit(
        run_id, "approve", values={"q1": "y2026", "q2": "unknown"}, seen=details["seen"]
    )
    assert view["status"] == "waiting" and view["pending"]["details"]["round"] == 2
    said = "「统计哪段时间？」选「只看 2026 年」（原建议「全部日期」）"
    assert f"【第 1 轮 · 确认方案 · 按我的意见修改方案】{said}" in lake.prompt()
    # The answered questions are rules now; the record of the first round is shown.
    pending = view["pending"]
    assert pending["fields"] is None
    data = pending["details"]["data"]
    assert data["previous"]["decision"] == "revise" and data["previous"]["round"] == 1
    assert data["brief"]["goal"] and data["returned"] is None
    assert any("只看 2026 年" in r["text"] for r in data["plan"]["rules"])

    view = await lake.submit(run_id, "approve", seen=pending["details"]["seen"])
    assert view["status"] == "waiting", view["error"]
    pending = view["pending"]
    assert view["step"]["node"] == "data_review"
    assert pending["options"] == ["approve", "fix_code", "fix_plan", "stop"]
    assert pending["changed_branch"] is None and pending["fields"] is None
    data = pending["details"]["data"]
    assert pending["details"]["view"] == "etl_dev_review"
    assert data["status"] == "passed" and data["answer"] is None
    assert data["quality"]["text"] == "1 条质量规则全部通过，丢弃 1 行"
    assert data["confirmed"]["decision"] == "approve" and data["confirmed"]["round"] == 2
    assert "src/pipeline.py" in data["files"]
    assert "## dev 运行通过" in pending["details"]["show"]

    view = await lake.submit(run_id, "approve")
    assert view["status"] == "done", view["error"]
    assert view["outcome"] == "delivered"
    result = view["result"]
    assert set(result) == {
        "report", "plan", "ingested", "brief", "answer", "code",
        "quality", "dev_url", "confirmed", "reviewed",
    }  # fmt: skip
    assert result["answer"] is None and result["dev_url"] == data["url"]
    assert sorted(result["code"]["files"]) == data["files"]
    assert (result["confirmed"]["actor"], result["reviewed"]["decision"]) == ("韩梅梅", "approve")
    assert [m["content"] for m in view["messages"] if m["node"] in ("review", "data_review")] == [
        "点了「确认方案，开始做」，改了答案或写了意见，按「按我的意见修改方案」处理；"
        "「统计哪段时间？」选了「只看 2026 年」（原建议「全部日期」）",
        "选了「确认方案，开始做」",
        "选了「通过」",
    ]
    listed = (await lake.api.get("/runs")).json()
    assert [r["outcome"] for r in listed if r["run_id"] == run_id] == ["delivered"]


async def test_code_sent_back_after_the_dev_run_counts_its_runs_again(make):
    lake = await make(
        [v2_plan(), GOOD_CODE, GOOD_CODE, GOOD_CODE],
        [FIXABLE, PASSED, PASSED],
        [INGESTED, PREVIEW, PREVIEW],
    )
    run_id, _ = await lake.start()
    view = await lake.submit(run_id, "approve", values=SUGGESTED)
    assert view["step"]["node"] == "data_review", view["error"]
    assert "- 第 2 次运行" in view["pending"]["details"]["show"]

    view = await lake.submit(run_id, "fix_code", comment="数量列改成整数")
    assert view["status"] == "waiting", view["error"]
    assert "【第 1 轮 · 审核试算结果 · 改代码】意见：数量列改成整数" in lake.prompt()
    pending = view["pending"]
    assert view["step"]["node"] == "data_review" and pending["details"]["round"] == 2
    # A new round of the dev run, counted from 1; the tables were not copied again.
    assert "- 第 1 次运行" in pending["details"]["show"]
    assert len(lake.ws.commands) == 3

    view = await lake.submit(run_id, "approve")
    assert view["status"] == "done" and view["outcome"] == "delivered"
    assert view["result"]["reviewed"]["round"] == 2
    # Only the code was written again: one plan, one confirmation, one extraction.
    assert [steps(view).count(n) for n in ("check_plan", "review", "ingest")] == [1, 1, 1]
    assert len(lake.gateway.requests) == 4


@pytest.mark.parametrize("reuse", [True, None])
async def test_plan_sent_back_after_the_dev_run_is_confirmed_again(make, reuse):
    commands = [INGESTED, PREVIEW] + ([] if reuse else [INGESTED]) + [PREVIEW]
    lake = await make(
        [v2_plan(), GOOD_CODE, v2_plan(summary="去掉退货。"), GOOD_CODE],
        [PASSED, PASSED],
        commands,
        reuse=reuse,
    )
    run_id, _ = await lake.start()
    view = await lake.submit(run_id, "approve", values=SUGGESTED)
    assert view["step"]["node"] == "data_review", view["error"]

    # Only a note on a column: enough for 改方案, and it goes to the model.
    note = {"item": "column:order_facts.quantity", "text": "数量要去掉退货"}
    view = await lake.submit(run_id, "fix_plan", notes=[note])
    assert view["status"] == "waiting", view["error"]
    assert (
        "【第 1 轮 · 审核试算结果 · 改方案】列「order_facts.quantity」：数量要去掉退货"
        in lake.prompt()
    )
    pending = view["pending"]
    assert view["step"]["node"] == "review" and pending["details"]["round"] == 2
    data = pending["details"]["data"]
    assert data["returned"]["decision"] == "fix_plan"
    # A column is named by its id (flow-apps.md: the third way of finding an item).
    assert data["returned"]["notes"][0]["label"] == "order_facts.quantity"
    assert data["previous"]["decision"] == "approve"
    # Both questions were answered in the first round.
    assert pending["fields"] is None

    view = await lake.submit(run_id, "approve")
    assert view["step"]["node"] == "data_review", view["error"]
    ingest = [m["content"] for m in view["messages"] if m["node"] == "ingest"]
    if reuse:
        assert ingest[-1] == "要的 2 张表这次运行已经抽取过，沿用（共 6 行）"
        assert len(lake.ws.commands) == 3
    else:
        assert ingest[-1].startswith("已把 2 张表共 6 行抽取到 Databricks")
        assert len(sent_spec(lake.ws.commands[2]["command"], "ingest")["tables"]) == 2
        assert len(lake.ws.commands) == 4
    view = await lake.submit(run_id, "approve")
    assert view["outcome"] == "delivered"
    assert view["result"]["confirmed"]["round"] == 2


async def test_stopped_with_only_a_note_gives_it_as_the_reason(make):
    lake = await make([v2_plan(), GOOD_CODE], [PASSED], [INGESTED, PREVIEW])
    run_id, _ = await lake.start()
    await lake.submit(run_id, "approve", values=SUGGESTED)
    note = {"item": "column:order_facts.city", "text": "城市要按口径归一，这版不能用"}
    view = await lake.submit(run_id, "stop", notes=[note])
    assert view["status"] == "done", view["error"]
    assert view["outcome"] == "stopped"
    result = view["result"]
    assert set(result) == {"reason", "brief", "plan", "report", "confirmed", "reviewed"}
    assert (
        result["reason"]
        == "【第 1 轮 · 审核试算结果 · 终止】列「order_facts.city」：城市要按口径归一，这版不能用"
    )
    assert view["messages"][-1]["node"] == "data_review"


async def test_a_failed_dev_run_cannot_be_passed(make):
    failed = {"write_errors": ["order_facts: 写表失败：没有权限"]}
    lake = await make([v2_plan(), GOOD_CODE], [failed], [INGESTED])
    run_id, _ = await lake.start()
    view = await lake.submit(run_id, "approve", values=SUGGESTED)
    pending = view["pending"]
    assert view["step"]["node"] == "data_review", view["error"]
    assert pending["options"] == ["fix_code", "fix_plan", "stop"]
    assert pending["details"]["data"]["status"] == "failed"
    assert pending["details"]["data"]["quality"] is None

    refused = await lake.answer(run_id, "approve")
    assert refused.status_code == 422
    assert refused.json()["detail"] == "这一步现在不能选「approve」"
    # 终止 needs a word on why.
    assert (await lake.answer(run_id, "stop")).status_code == 422

    view = await lake.submit(run_id, "stop", comment="没有写表权限，先停下")
    assert (view["outcome"], view["result"]["reason"]) == ("stopped", "没有写表权限，先停下")
