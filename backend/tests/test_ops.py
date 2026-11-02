"""The operations dashboard and per-app model budgets (docs/api/ops.md)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aep import llm
from aep.compute import ComputeRecord
from aep.main import create_app
from aep.models import MemoryCallStore, MemorySettingStore
from aep.notify import MemoryNotificationStore, Notifier
from aep.ops import (
    MemoryBudgetStore,
    MemoryComputeStore,
    Ops,
    reuse_from,
)
from aep.runtime.store import MemoryRunStore, RunRecord

NOW = datetime(2026, 11, 20, 4, 0, tzinfo=UTC)  # 12:00 in Asia/Shanghai


def call(run_id: str | None, cost: float | None, tokens: int = 10) -> llm.CallRecord:
    return llm.CallRecord("mock", "chat", run_id, "plan", tokens, 2, cost, True, None, 5)


class Harness:
    def __init__(self) -> None:
        self.runs = MemoryRunStore()
        self.calls = MemoryCallStore()
        self.compute = MemoryComputeStore()
        self.budgets = MemoryBudgetStore()
        self.settings = MemorySettingStore()
        self.notices: list[dict] = []
        self.clock = 0.0
        self.ops = Ops(
            self.runs,
            self.calls,
            self.compute,
            self.budgets,
            self.settings,
            self.titles,
            reuse=self.reuse,
            notifier=self,  # type: ignore[arg-type]
            now=lambda: NOW,
            clock=lambda: self.clock,
        )

    async def titles(self) -> dict[str, str]:
        return {"etl": "数据开发", "qa": "问答", "idle": "闲置"}

    async def reuse(self) -> dict:
        return {"assets": 0, "reused": 0, "apps_from_templates": 0, "top": []}

    async def budget(self, app, title, run_id, **values) -> None:
        self.notices.append({"app": app, "title": title, "run_id": run_id, **values})

    def run(
        self,
        run_id: str,
        app: str,
        status: str,
        *,
        hours_ago: float = 1,
        minutes: float = 10,
        rerun_of: str | None = None,
        outcome: str | None = None,
        wait: float | None = 0,
        draft: bool = False,
    ) -> None:
        created = NOW - timedelta(hours=hours_ago)
        self.runs._runs[run_id] = RunRecord(
            run_id,
            app,
            status,  # type: ignore[arg-type]
            {},
            app_version=None if draft else 1,
            config={"x": 1} if draft else None,
            created_at=created,
            updated_at=created + timedelta(minutes=minutes),
            rerun_of=rerun_of,
            outcome=outcome,
            wait_seconds=wait,
        )

    def called(self, run_id: str | None, cost: float | None, days_ago: float = 0) -> None:
        self.calls.calls.append((NOW - timedelta(days=days_ago), call(run_id, cost)))


@pytest.fixture
def h() -> Harness:
    return Harness()


async def test_summary_counts_runs_delivery_first_pass_costs_and_compute(h):
    h.run("a1", "etl", "done", minutes=10, wait=300, outcome="delivered")
    h.run("a2", "etl", "error", minutes=2)
    h.run("a3", "etl", "done", minutes=30, rerun_of="a2", wait=None)
    h.run("a4", "etl", "done", minutes=20, outcome="stopped")
    h.run("a5", "etl", "waiting")
    h.run("q1", "qa", "done", minutes=1, hours_ago=30)
    h.run("q2", "qa", "done", minutes=1)
    h.run("q3", "qa", "cancelled")
    h.run("draft", "qa", "done", draft=True)  # a draft's trial run is not counted
    h.run("old", "etl", "done", hours_ago=24 * 40)  # before the window
    h.called("a1", 0.5)
    h.called("a1", None)
    h.called("q2", 0.25)
    h.called("old", 1.0)  # a call this month from a run created before the window
    h.called(None, 0.1)  # outside runs: totals only
    h.called("a1", 9.0, days_ago=40)  # before the window
    await h.compute.record(ComputeRecord("a1", "check", "job", "c1", 1800))
    await h.compute.record(ComputeRecord("a3", "check", "command", "c1", 1800))
    await h.ops.set_price(2.0)

    out = await h.ops.summary(30)
    totals = out["totals"]
    assert (totals["runs"], totals["done"], totals["error"], totals["cancelled"]) == (8, 5, 1, 1)
    assert totals["active"] == 1
    assert totals["model_cost_usd"] == pytest.approx(1.85)
    assert (totals["model_calls"], totals["unpriced_calls"]) == (5, 1)
    assert totals["compute_seconds"] == 3600
    assert totals["compute_cost_usd"] == 2.0
    assert out["compute_hourly_usd"] == 2.0

    etl = next(a for a in out["apps"] if a["app"] == "etl")
    assert etl["title"] == "数据开发" and etl["runs"] == 5
    # Done runs took 10, 30 and 20 minutes.
    assert etl["delivery_seconds_median"] == 1200
    assert etl["wait_seconds_median"] == 150  # 300 and 0; a3 never counted its wait
    # First attempts that finished: a1 (passed), a2 (failed, rerun), a4 (stopped).
    assert etl["first_pass_rate"] == 0.3333
    assert etl["model_cost_usd"] == pytest.approx(1.5)  # a1 and the old run's call
    assert etl["compute_seconds"] == 3600 and etl["budget"] is None
    qa = next(a for a in out["apps"] if a["app"] == "qa")
    assert qa["runs"] == 3 and qa["first_pass_rate"] == 0.6667
    assert qa["model_cost_usd"] == 0.25 and qa["compute_cost_usd"] == 0.0
    assert [a["app"] for a in out["apps"]] == ["etl", "qa"]

    daily = out["daily"]
    assert len(daily) == 30 and daily[-1]["date"] == "2026-11-20"
    assert daily[-1]["runs"] == 7 and daily[-1]["done"] == 4
    assert daily[-2]["runs"] == 1  # q1, 30 hours ago
    assert daily[-1]["model_cost_usd"] == pytest.approx(1.85)
    assert daily[0]["model_cost_usd"] is None
    assert out["reuse"]["assets"] == 0

    week = await h.ops.summary(7)
    assert week["totals"]["runs"] == 8 and len(week["daily"]) == 7


async def test_no_price_means_no_compute_cost(h):
    h.run("a1", "etl", "done")
    await h.compute.record(ComputeRecord("a1", None, "job", None, 60))
    out = await h.ops.summary(30)
    assert out["totals"]["compute_seconds"] == 60
    assert out["totals"]["compute_cost_usd"] is None
    assert out["totals"]["model_cost_usd"] is None
    assert out["totals"]["first_pass_rate"] == 1


async def test_budgets_warn_then_block_and_notify_once_a_month(h):
    h.run("a1", "etl", "running")
    h.run("q1", "qa", "running")
    budget = await h.ops.set_budget("etl", 10, 0.8, True, "张三")
    assert budget == {
        "monthly_usd": 10,
        "warn_at": 0.8,
        "block": True,
        "month_cost_usd": 0,
        "state": "ok",
        "updated_by": "张三",
        "updated_at": budget["updated_at"],
    }
    await h.ops.guard("a1")
    await h.ops.guard("q1")  # no budget
    await h.ops.guard(None)  # outside runs
    assert h.notices == []

    h.called("a1", 8.5)
    h.called("a1", 9.0, days_ago=25)  # last month: 2026-10-26
    h.clock += 31  # past the cache
    await h.ops.guard("a1")
    await h.ops.guard("a1")
    assert [(n["over"], n["spent"]) for n in h.notices] == [(False, 8.5)]
    assert (await h.ops.budget("etl"))["state"] == "warn"
    assert (await h.budgets.get("etl")).warned_month == "2026-11"

    h.called("a1", 2.0)
    await h.ops.guard("a1")  # still the cached 8.5
    h.clock += 31
    with pytest.raises(llm.BudgetExceeded) as raised:
        await h.ops.guard("a1")
    assert str(raised.value) == (
        "应用「数据开发」本月的模型预算 $10 已经用完，请管理员在运营看板调高预算或下月再用"
    )
    with pytest.raises(llm.BudgetExceeded):
        await h.ops.guard("a1")
    assert [n["over"] for n in h.notices] == [False, True]
    out = await h.ops.summary(30)
    etl = next(a for a in out["apps"] if a["app"] == "etl")
    assert etl["budget"]["state"] == "over"
    assert etl["budget"]["month_cost_usd"] == pytest.approx(10.5)

    # Only warning: calls go on. Raising the budget lets them through at once.
    await h.ops.set_budget("etl", 10, 0.8, False, None)
    await h.ops.guard("a1")
    assert len(h.notices) == 2  # same amount: no second notice this month
    await h.ops.set_budget("etl", 12, 0.8, True, None)
    await h.ops.guard("a1")  # a new amount starts the notices over: 10.5 of 12 warns
    assert [n["over"] for n in h.notices] == [False, True, False]
    await h.ops.set_budget("etl", 50, 0.8, True, None)
    await h.ops.guard("a1")
    await h.ops.delete_budget("etl")
    assert await h.ops.budget("etl") is None
    await h.ops.guard("a1")


async def test_the_guard_runs_before_model_calls(monkeypatch, gateway):
    blocked: list[str | None] = []

    async def guard(run_id: str | None) -> None:
        blocked.append(run_id)
        raise llm.BudgetExceeded("用完了")

    monkeypatch.setattr(llm, "run_context", lambda: ("r1", "plan"))
    llm.set_guard(guard)
    try:
        with pytest.raises(llm.BudgetExceeded):
            await llm.chat(gateway.client, [{"role": "user", "content": "hi"}], "mock")
        with pytest.raises(llm.BudgetExceeded):
            await llm.embed(gateway.client, ["hi"], "company-embed")
        assert await llm.embed(gateway.client, ["hi"], "mock-embed")  # never leaves
    finally:
        llm.set_guard(None)
    assert blocked == ["r1", "r1"]


async def test_budget_notices_go_to_admins_inbox_and_webhooks():
    from aep.config import Settings

    store = MemoryNotificationStore()
    sent: list[tuple[str, dict]] = []

    async def post(url: str, body: dict):
        sent.append((url, body))
        return 200, {}

    notifier = Notifier(store, Settings(), post=post)
    await notifier.create_webhook(
        "群", "generic", "https://hook.test/x", ["app.budget"], None, True
    )
    await notifier.budget("etl", "数据开发", "r1", over=True, spent=10.5, monthly=10, block=True)
    [(_, body)] = sent
    assert body["event"] == "app.budget" and body["title"] == "「数据开发」本月模型预算已用完"
    assert body["body"] == "本月已用 $10.50，预算 $10；之后这个应用的模型调用会被拦下。"
    assert body["link"].endswith("/ops")
    inbox, unread = await store.page(None, 10, None, False)
    assert inbox[0]["event"] == "app.budget" and inbox[0]["link"] == "/ops" and unread == 1


def test_reuse_from_catalog_items():
    def item(kind, key, apps):
        return {
            "kind": kind,
            "id": key,
            "title": key,
            "link": f"/{key}",
            "used_by": [{"app": a} for a in apps],
        }

    out = reuse_from(
        [
            item("template", "t", ["a", "b"]),
            item("skill", "s", ["a", "b", "b"]),
            item("knowledge", "k", ["a"]),
            item("tool", "x", []),
        ]
    )
    assert out == {
        "assets": 3,
        "reused": 1,
        "apps_from_templates": 2,
        "top": [
            {"kind": "skill", "id": "s", "title": "s", "link": "/s", "used_by": 2},
            {"kind": "knowledge", "id": "k", "title": "k", "link": "/k", "used_by": 1},
        ],
    }


@pytest.fixture
async def client(h):
    app = create_app()
    app.state.ops = h.ops
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_api(client, h):
    assert (await client.get("/ops/summary?days=14")).status_code == 422
    assert (await client.get("/ops/summary")).json()["days"] == 30
    saved = await client.put("/ops/settings", json={"compute_hourly_usd": 3.5})
    assert saved.json() == {"compute_hourly_usd": 3.5}
    assert (await client.put("/ops/settings", json={"compute_hourly_usd": -1})).status_code == 422
    cleared = await client.put("/ops/settings", json={"compute_hourly_usd": None})
    assert cleared.json() == {"compute_hourly_usd": None}

    assert (await client.get("/apps/etl/budget")).json() is None
    assert (await client.get("/apps/nope/budget")).status_code == 404
    bad = await client.put("/apps/etl/budget", json={"monthly_usd": 0})
    assert bad.status_code == 422
    put = await client.put(
        "/apps/etl/budget", json={"monthly_usd": 20, "block": False, "actor": "李四"}
    )
    assert put.status_code == 200, put.text
    assert put.json()["warn_at"] == 0.8 and put.json()["updated_by"] == "李四"
    assert (await client.get("/apps/etl/budget")).json()["monthly_usd"] == 20
    out = (await client.get("/ops/summary?days=7")).json()
    assert [a["app"] for a in out["apps"]] == ["etl"]  # listed for its budget
    assert out["apps"][0]["budget"]["block"] is False
    assert (await client.delete("/apps/etl/budget")).status_code == 204
    assert (await client.get("/apps/etl/budget")).json() is None
