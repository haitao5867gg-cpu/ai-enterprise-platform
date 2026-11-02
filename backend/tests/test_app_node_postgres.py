"""Finding the run an app node started, on real Postgres (a restarted caller waits for
it instead of starting another one).

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
import uuid

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import PostgresRunStore

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


async def test_called_by_finds_the_unfinished_run_a_node_started():
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    try:
        runs = PostgresRunStore(pool)
        await runs.setup()
        app = f"called-{uuid.uuid4().hex[:8]}"
        caller = await runs.create(app, {})
        trigger = {"kind": "app", "run_id": caller.run_id, "node": "call", "title": "前台"}
        older = await runs.create(app, {}, trigger=trigger)
        newer = await runs.create(app, {}, trigger=trigger)
        found = await runs.called_by(caller.run_id, "call")
        assert found is not None and found.run_id == newer.run_id
        assert await runs.called_by(caller.run_id, "other") is None
        await runs.set_status(newer.run_id, "done")
        found = await runs.called_by(caller.run_id, "call")
        assert found is not None and found.run_id == older.run_id
        # Finished, so the recovery tests sharing the database do not pick them up.
        for made in (caller, older):
            await runs.set_status(made.run_id, "done")
        assert await runs.called_by(caller.run_id, "call") is None
    finally:
        await pool.close()
