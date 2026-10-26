"""Runtime on real Postgres, including surviving a restart while waiting for approval.

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
from contextlib import asynccontextmanager

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
        record = await first._store.create("hello", {"text": "你好"})

    async with platform(gateway) as second:
        assert await second.recover() >= 1
        await second.wait_idle()
        view = await second.get(record.run_id)
        assert view.record.status == "waiting"
        assert view.messages[-1] == {"role": "assistant", "content": "恢复后的回答"}
