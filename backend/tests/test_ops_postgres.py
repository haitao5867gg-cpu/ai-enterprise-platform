"""The operations dashboard's queries on real Postgres: call rollups, run stats, compute
usage and budgets. Runs when AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.compute import ComputeRecord
from aep.llm import CallRecord
from aep.models import PostgresCallStore
from aep.ops import Budget, PostgresOpsStore
from aep.runtime.store import PostgresRunStore

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    yield pool
    await pool.close()


async def test_calls_roll_up_per_run_and_local_day(pool):
    store = PostgresCallStore(pool)
    await store.setup()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    for cost in (0.5, None, 0.25):
        await store.record(CallRecord("m", "chat", run_id, "plan", 10, 2, cost, True, None, 5))
    since = datetime.now(UTC) - timedelta(minutes=5)

    rows = [row for row in await store.rollup(since, "Asia/Shanghai") if row.run_id == run_id]

    assert len(rows) == 1
    row = rows[0]
    assert (row.calls, row.unpriced, row.prompt_tokens, row.completion_tokens) == (3, 1, 30, 6)
    assert float(row.cost_usd) == pytest.approx(0.75)
    # Days are counted in the platform timezone (a call just made is "today" there).
    assert row.day == datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def test_run_stats_skip_draft_runs_and_mark_reruns(pool):
    store = PostgresRunStore(pool)
    await store.setup()
    app = f"ops-{uuid.uuid4().hex[:8]}"
    since = datetime.now(UTC) - timedelta(minutes=5)
    first = await store.create(app, {}, app_version=1)
    await store.set_status(first.run_id, "running")
    await store.set_status(first.run_id, "error")
    rerun = await store.create(app, {}, app_version=1, rerun_of=first.run_id)
    await store.set_status(rerun.run_id, "running")
    await store.set_status(rerun.run_id, "done", outcome="delivered")
    # A draft's trial run, finished so that no other test recovers it.
    draft = await store.create(app, {}, config={"x": 1})
    await store.set_status(draft.run_id, "cancelled")

    stats = {stat.run_id: stat for stat in await store.stats(since) if stat.app == app}

    assert set(stats) == {first.run_id, rerun.run_id}
    assert stats[first.run_id].reran and stats[first.run_id].status == "error"
    assert not stats[rerun.run_id].reran
    assert stats[rerun.run_id].rerun_of == first.run_id
    assert stats[rerun.run_id].outcome == "delivered"
    assert await store.apps_of([first.run_id, "not-a-uuid", str(uuid.uuid4())]) == {
        first.run_id: app
    }
    assert await store.apps_of([]) == {}


async def test_compute_usage_and_budgets(pool):
    store = PostgresOpsStore(pool)
    await store.setup()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    since = datetime.now(UTC) - timedelta(minutes=5)
    await store.record(ComputeRecord(run_id, "check", "job", "c1", 90))
    await store.record(ComputeRecord(run_id, "check", "command", "c1", 1.5))
    assert (await store.by_run(since))[run_id] == pytest.approx(91.5)

    app = f"ops-{uuid.uuid4().hex[:8]}"
    assert await store.get(app) is None
    saved = await store.put(Budget(app, 10, 0.8, True, "王工"))
    assert (saved.monthly_usd, saved.warn_at, saved.block, saved.updated_by) == (
        10,
        0.8,
        True,
        "王工",
    )
    assert saved.updated_at is not None and saved.warned_month is None
    await store.mark(app, warned_month="2026-11")
    await store.mark(app, over_month="2026-11")
    kept = await store.put(Budget(app, 10, 0.8, False, "李工"))
    # Only "block" changed: this month's notices are not sent twice.
    assert kept.block is False and (kept.warned_month, kept.over_month) == ("2026-11", "2026-11")
    changed = await store.put(Budget(app, 20, 0.8, False, "李工"))
    # A new amount starts the month's notices over.
    assert changed.monthly_usd == 20 and (changed.warned_month, changed.over_month) == (None, None)
    assert app in [budget.app for budget in await store.all()]
    with pytest.raises(ValueError):
        await store.mark(app, monthly_usd="1")
    await store.delete(app)
    assert await store.get(app) is None
