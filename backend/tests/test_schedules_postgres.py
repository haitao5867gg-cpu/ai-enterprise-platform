"""The schedule store on real Postgres: saving and reading back, two processes claiming
due schedules at once start each only once, and runs keep their trigger.

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import PostgresRunStore
from aep.schedules import PostgresScheduleStore, Schedule

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


async def test_schedule_store_and_claiming_once():
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        min_size=2,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    app = f"sched-{uuid.uuid4().hex[:8]}"
    try:
        store = PostgresScheduleStore(pool)
        await store.setup()
        await store.setup()  # Twice is fine.
        due = Schedule(
            schedule_id=f"sch_{uuid.uuid4().hex[:8]}",
            app=app,
            title="每天早上的日报",
            cron="0 8 * * *",
            timezone="Asia/Shanghai",
            input={"question": "日报"},
            project={"project_id": "jade-ibf", "environment": "dev", "params": {}},
            enabled=True,
            created_by="张三",
            created_at=NOW,
            updated_at=NOW,
            next_run_at=NOW - timedelta(minutes=1),
        )
        off = Schedule(**{**vars(due), "schedule_id": f"sch_{uuid.uuid4().hex[:8]}"})
        off.enabled, off.created_at = False, NOW + timedelta(seconds=1)
        await store.put(due)
        await store.put(off)
        assert await store.get(due.schedule_id) == due
        assert [s.schedule_id for s in await store.for_app(app)] == [
            due.schedule_id,
            off.schedule_id,
        ]

        # Two processes at once: one of them gets it, moved on to 08:00 tomorrow.
        first, second = await asyncio.gather(store.claim_due(NOW), store.claim_due(NOW))
        mine = [s for s in first + second if s.app == app]
        assert [s.schedule_id for s in mine] == [due.schedule_id]
        tomorrow = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
        assert mine[0].next_run_at == tomorrow
        assert not [s for s in await store.claim_due(NOW) if s.app == app]

        await store.started(due.schedule_id, "run-1", NOW, None)
        await store.started(due.schedule_id, None, NOW, "上一次运行还没结束，这次跳过")
        found = await store.get(due.schedule_id)
        assert found is not None
        assert (found.last_run_id, found.last_run_at, found.next_run_at) == ("run-1", NOW, tomorrow)
        assert found.last_error == "上一次运行还没结束，这次跳过"
        # Saving a change keeps what the scheduler recorded.
        await store.put(Schedule(**{**vars(due), "title": "日报", "last_run_id": None}))
        changed = await store.get(due.schedule_id)
        assert changed is not None and changed.title == "日报"
        assert changed.last_run_id == "run-1"

        assert await store.delete(off.schedule_id)
        assert not await store.delete(off.schedule_id)
        await store.delete_app(app)
        assert await store.for_app(app) == []

        runs = PostgresRunStore(pool)
        await runs.setup()
        trigger = {"kind": "schedule", "schedule_id": due.schedule_id, "title": "日报"}
        record = await runs.create(app, {"question": "日报"}, created_by="张三", trigger=trigger)
        assert record.trigger == trigger
        assert (await runs.get(record.run_id)).trigger == trigger  # type: ignore[union-attr]
        plain = await runs.create(app, {})
        assert plain.trigger is None
        # Finished, so the recovery tests sharing the database do not pick them up.
        for made in (record, plain):
            await runs.set_status(made.run_id, "done")
    finally:
        await store.delete_app(app)
        await pool.close()
