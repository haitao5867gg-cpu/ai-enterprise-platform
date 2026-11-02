"""The asset catalog's records and run outcomes on real Postgres. Runs when
AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.asset_catalog import AssetMeta, PostgresAssetMetaStore
from aep.runtime.store import PostgresRunStore

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


async def _pool() -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    return pool


async def test_owner_tags_and_pull_request_are_kept():
    pool = await _pool()
    try:
        store = PostgresAssetMetaStore(pool)
        await store.setup()
        await store.setup()
        name = f"skill-{uuid.uuid4().hex[:8]}"
        await store.put(AssetMeta("skill", name, "王五", ["客服"], None, "张三"))
        await store.put(AssetMeta("skill", name, None, ["售后"], {"number": 2}, "李四"))
        (found,) = [m for m in await store.all() if m.asset_id == name]
        assert (found.owner, found.tags, found.repo, found.updated_by) == (
            None,
            ["售后"],
            {"number": 2},
            "李四",
        )
        await store.delete("skill", name)
        assert [m for m in await store.all() if m.asset_id == name] == []
    finally:
        await pool.close()


async def test_run_outcomes_per_app():
    pool = await _pool()
    try:
        store = PostgresRunStore(pool)
        await store.setup()
        app = f"app-{uuid.uuid4().hex[:8]}"
        since = datetime.now(UTC) - timedelta(minutes=1)
        # Unfinished runs are left out too, but one would be resumed by other tests.
        for status in ("done", "done", "error", "cancelled"):
            run = await store.create(app, {})
            await store.set_status(run.run_id, status)
        assert (await store.outcomes(since))[app] == (2, 1)
        assert app not in await store.outcomes(datetime.now(UTC) + timedelta(minutes=1))
    finally:
        await pool.close()
