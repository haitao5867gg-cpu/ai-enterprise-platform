"""Data source store on real Postgres. Runs when AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.datasources import DuplicateName, PostgresDataSourceStore

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@pytest.fixture
async def store():
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    store = PostgresDataSourceStore(pool)
    await store.setup()
    yield store
    await pool.close()


async def test_create_get_and_list(store):
    name = f"shop-{uuid.uuid4().hex[:8]}"
    ds = await store.create(name, "postgres", "http://connector:8080/mcp", "示例库")

    assert await store.get(ds.datasource_id) == ds
    assert name in [d.name for d in await store.all()]
    assert await store.get("not-a-uuid") is None
    with pytest.raises(DuplicateName):
        await store.create(name, "mysql", "http://other:8080/mcp")
