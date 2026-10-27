"""GET /runs/{id}/flow: how an app is built and the steps a run took."""

import httpx

from aep.main import app
from aep.runtime.apps import etl
from tests.test_etl import PYSPARK_BODY, decide, make_service, plan, reply, start


async def get_flow(service, run_id: str) -> httpx.Response:
    app.state.run_service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        return await api.get(f"/runs/{run_id}/flow")


def path(flow: dict) -> list[tuple[str, str]]:
    return [(s["node"], s["status"]) for s in flow["steps"]]


async def test_etl_flow_shows_design_and_every_step():
    bad_code = {"targets": {"order_facts": 'return spark.sql("SELECT 1")'}}
    service, _, ds = await make_service(
        [
            reply(plan()),
            reply(plan(summary="按意见改。")),
            reply(bad_code),
            reply({"targets": {"order_facts": PYSPARK_BODY}}),
        ]
    )
    run_id = await start(service, ds, models={"codegen": "qwen"})
    await decide(service, run_id, "reject", "先做全量")

    resp = await get_flow(service, run_id)
    assert resp.status_code == 200
    flow = resp.json()
    assert flow["current"] == "review"
    assert path(flow) == [
        ("profile", "done"), ("plan", "done"), ("review", "done"),
        ("plan", "done"), ("review", "waiting"),
    ]  # fmt: skip
    waiting = flow["steps"][-1]
    assert waiting["output"] is None
    assert waiting["interrupt"]["details"]["type"] == "etl_plan"
    # A step sees the state before it ran, without the conversation, and shows what it wrote.
    profile, first_plan = flow["steps"][0], flow["steps"][1]
    assert profile["input"]["input"]["request"] == "做一张订单明细宽表"
    assert "messages" not in profile["input"]
    assert profile["output"]["profile"][0]["name"].startswith("sales.")
    assert first_plan["output"]["plan"]["targets"][0]["name"] == "order_facts"
    assert flow["steps"][2]["output"]["approvals"][0]["comment"] == "先做全量"

    await decide(service, run_id, "approve")
    flow = (await get_flow(service, run_id)).json()
    assert flow["current"] is None
    assert path(flow)[-3:] == [("codegen", "done"), ("codegen", "done"), ("package", "done")]

    assert flow["design"]["title"] == etl.DESIGN.title
    assert "知识库" in flow["design"]["knowledge"]
    nodes = {n["id"]: n for n in flow["nodes"]}
    assert list(nodes) == ["__start__", *etl.NODES, "__end__"]
    assert nodes["__start__"]["kind"] == "terminal"
    assert nodes["review"]["kind"] == "human"
    assert nodes["profile"]["design"]["tools"][0].startswith("数据源连接器")
    # The model a node uses is this run's choice for its step, or the default one.
    assert nodes["plan"]["model"] == "mock"
    assert nodes["codegen"]["model"] == "qwen"
    assert nodes["codegen"]["design"]["prompt"].startswith("你是资深 Databricks 数据工程师")
    assert nodes["profile"].get("model") is None
    edges = {(e["source"], e["target"]): e["conditional"] for e in flow["edges"]}
    assert edges[("profile", "plan")] is False
    assert edges[("review", "plan")] is True
    assert edges[("verify", "codegen")] is True
    assert edges[("release", "codegen")] is True


async def test_failed_step_is_marked():
    service, _, ds = await make_service(["这不是 JSON"] * 3)
    run_id = await start(service, ds)

    flow = (await get_flow(service, run_id)).json()
    assert flow["current"] == "plan"
    assert path(flow) == [("profile", "done"), ("plan", "error")]
    assert "方案不可用" in flow["steps"][-1]["error"]


async def test_unknown_run():
    service, _, _ = await make_service([])
    assert (await get_flow(service, "nope")).status_code == 404
