"""App entries and runs' entries on real Postgres (docs/api/app-access.md, "入口").

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.entries import Entries, PostgresEntryStore
from aep.feedback import Feedback, PostgresFeedbackStore
from aep.runtime.store import PostgresRunStore

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


async def pool() -> AsyncConnectionPool:
    found = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await found.open(wait=True)
    return found


async def test_entries_and_run_entries_are_kept():
    db = await pool()
    try:
        app = f"app-{uuid.uuid4().hex[:8]}"
        entries = PostgresEntryStore(db)
        await entries.setup()
        assert vars(await entries.get(app)) | {"updated_at": None} == vars(Entries(app)) | {
            "updated_at": None
        }
        await entries.put(Entries(app, mcp=False, apps=False, updated_by="王搭建"))
        found = await entries.get(app)
        assert (found.use, found.api, found.mcp, found.apps) == (True, True, False, False)
        assert found.updated_by == "王搭建"

        runs = PostgresRunStore(db)
        await runs.setup()
        first = await runs.create(app, {}, created_by="李雷", entry="use")
        second = await runs.create(
            "other-app",
            {},
            trigger={"kind": "app", "run_id": first.run_id, "node": "call", "title": "x"},
            entry="apps",
        )
        assert first.entry == "use" and second.entry == "apps"
        for record in (first, second):  # not left running for the recovery tests
            await runs.set_status(record.run_id, "done")
        since = datetime.now(UTC) - timedelta(minutes=5)
        assert [r.run_id for r in await runs.of_app(app, since, 10)] == [first.run_id]
        assert second.run_id in {r.run_id for r in await runs.called_since(since)}

        feedback = PostgresFeedbackStore(db)
        await feedback.setup()
        await feedback.put(Feedback(first.run_id, "", "good"))
        assert [f.rating for f in await feedback.for_runs([first.run_id, second.run_id])] == [
            "good"
        ]
        assert await feedback.for_runs([]) == []
    finally:
        await db.close()
