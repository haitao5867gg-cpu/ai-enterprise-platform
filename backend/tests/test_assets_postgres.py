"""Asset index on real Postgres. Runs when AEP_TEST_DATABASE_URL is set (CI sets it)."""

import asyncio
import os
import uuid

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.assets import PostgresAssetStore, Published

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@pytest.fixture
async def store():
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    store = PostgresAssetStore(pool)
    await store.setup()
    yield store
    await pool.close()


async def test_versions_by_name(store):
    name = f"shop_{uuid.uuid4().hex[:8]}"
    assert await store.reserve(name, "run-1", "r1") == 1
    assert await store.reserve(name, "run-1", "r1") == 1  # the same step again
    assert await store.reserve(name, "run-2", "r1") == 2
    (pending,) = [a for a in await store.find(name) if a.version == 1]
    assert (pending.status, pending.pr_number, pending.branch) == ("pending", None, "")

    first = await store.record(name, 1, Published(7, "https://pr/7", f"aep/{name}/v1", "a1"))
    assert (first.version, first.status, first.pr_number, first.run_id) == (
        1,
        "proposed",
        7,
        "run-1",
    )
    # Recording again (a retried step) keeps one row, with the latest commit.
    again = await store.record(name, 1, Published(7, "https://pr/7", f"aep/{name}/v1", "a2"))
    assert (again.asset_id, again.commit_sha) == (first.asset_id, "a2")

    await store.record(name, 2, Published(9, "https://pr/9", f"aep/{name}/v2", "b1"))
    await store.set_status(name, 1, "rejected")
    found = [(a.version, a.status, a.run_id) for a in await store.find(name)]
    assert found == [(2, "proposed", "run-2"), (1, "rejected", "run-1")]
    assert name in {a.name for a in await store.find()}
    assert await store.find(f"{name}_other") == []


async def test_concurrent_runs_get_different_versions(store):
    name = f"shop_{uuid.uuid4().hex[:8]}"

    versions = await asyncio.gather(*(store.reserve(name, f"run-{i}", "r") for i in range(8)))

    assert sorted(versions) == list(range(1, 9))
