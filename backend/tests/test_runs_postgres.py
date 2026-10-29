"""Runtime on real Postgres, including surviving a restart while waiting for approval.

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import PostgresRunStore
from tests.conftest import SETTINGS, FakeGateway

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@asynccontextmanager
async def platform(gateway: FakeGateway):
    """One 'process': its own pool, checkpointer, store and service."""
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        store = PostgresRunStore(pool)
        await store.setup()
        yield RunService(
            store,
            checkpointer,
            default_apps(),
            AppContext(llm_client=gateway.client, settings=SETTINGS),
        )
    finally:
        await pool.close()


async def test_run_metadata_is_kept():
    gateway = FakeGateway(replies=["回答"])

    async with platform(gateway) as service:
        record = await service.create("hello", {"text": "你好"}, requester="李雷")
        await service.wait_idle()
        waiting = (await service.get(record.run_id)).record
        assert waiting.created_by == "李雷"
        assert waiting.step == {"node": "review", "title": "确认"}
        assert waiting.waiting_since is not None
        assert waiting.wait_seconds == 0

        async with service._store._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET waiting_since = now() - interval '2 minutes' WHERE run_id = %s",
                (UUID(record.run_id),),
            )
        await service.resume(record.run_id, "approve", None)
        await service.wait_idle()
        done = (await service.get(record.run_id)).record
        assert done.status == "done"
        assert done.step == {"node": "end", "title": "结束"}
        assert done.waiting_since is None
        assert 120 <= done.wait_seconds < 130

        # A run from before waits were counted.
        async with service._store._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET wait_seconds = NULL WHERE run_id = %s", (UUID(record.run_id),)
            )
        await service._store.set_status(record.run_id, "waiting")
        await service._store.set_status(record.run_id, "done")
        assert (await service._store.get(record.run_id)).wait_seconds is None


async def test_waiting_run_survives_restart():
    gateway = FakeGateway(replies=["持久化的回答"])

    async with platform(gateway) as first:
        record = await first.create("hello", {"text": "你好"})
        await first.wait_idle()
        assert (await first.get(record.run_id)).record.status == "waiting"

    # A new process with nothing in memory picks the run up from Postgres.
    async with platform(gateway) as second:
        view = await second.get(record.run_id)
        assert view.record.status == "waiting"
        assert view.pending["kind"] == "approval"

        await second.resume(record.run_id, "approve", None)
        await second.wait_idle()

        done = await second.get(record.run_id)
        assert done.record.status == "done"
        assert done.result == {"answer": "持久化的回答"}
        assert record.run_id in [r.run_id for r in await second.recent()]


async def test_recover_restarts_runs_left_running():
    gateway = FakeGateway(replies=["恢复后的回答"])

    async with platform(gateway) as first:
        # Simulate a crash right after the record was written, before any step ran.
        record = await first._store.create("hello", {"text": "你好"}, created_by="李雷")

    async with platform(gateway) as second:
        assert await second.recover() >= 1
        await second.wait_idle()
        view = await second.get(record.run_id)
        assert view.record.status == "waiting"
        assert [(m["node"], m["actor"], m["content"]) for m in view.messages] == [
            ("start", "李雷", "你好"),
            ("draft", None, "恢复后的回答"),
        ]


async def test_status_changes_only_from_the_expected_status():
    gateway = FakeGateway(replies=["回答"])

    async with platform(gateway) as service:
        record = await service.create("hello", {"text": "你好"}, requester="李雷")
        await service.wait_idle()
        store = service._store
        assert not await store.set_status(record.run_id, "running", expected=("error",))
        assert (await store.get(record.run_id)).status == "waiting"

        await service.cancel(record.run_id, "韩梅梅", "不需要了")
        cancelled = await store.get(record.run_id)
        assert cancelled.status == "cancelled"
        assert cancelled.cancelled_by == "韩梅梅"
        assert cancelled.error == "不需要了"
        assert not await store.set_status(record.run_id, "running", expected=("waiting",))

        rerun = await service.rerun(record.run_id)
        await service.wait_idle()
        assert (await store.get(rerun.run_id)).rerun_of == record.run_id

        await store.set_status(rerun.run_id, "error", "超时", error_type="timeout")
        failed = await store.get(rerun.run_id)
        assert (failed.status, failed.error_type) == ("error", "timeout")
        # Nobody retried it for an hour: that hour counts as waiting, not as work.
        async with store._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET updated_at = updated_at - interval '1 hour' WHERE run_id = %s",
                (UUID(rerun.run_id),),
            )
        await service.retry(rerun.run_id)
        assert (await store.get(rerun.run_id)).wait_seconds >= failed.wait_seconds + 3600
        await service.wait_idle()
        retried = await store.get(rerun.run_id)
        assert (retried.status, retried.error, retried.error_type) == ("waiting", None, None)
