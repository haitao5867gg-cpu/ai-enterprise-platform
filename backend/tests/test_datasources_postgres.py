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


async def test_update_renames_and_moves(store):
    name = f"shop-{uuid.uuid4().hex[:8]}"
    other = f"crm-{uuid.uuid4().hex[:8]}"
    ds = await store.create(name, "postgres", "http://connector:8080/mcp")
    taken = await store.create(other, "mcp", "http://tools:8080/mcp")

    renamed = await store.update(ds.datasource_id, name=f"{name}-2", description="订单和客户")
    assert (renamed.name, renamed.description) == (f"{name}-2", "订单和客户")
    assert renamed.connector_url == ds.connector_url and renamed.kind == "postgres"
    moved = await store.update(ds.datasource_id, connector_url="http://connector:9090/mcp")
    assert moved.connector_url == "http://connector:9090/mcp" and moved.name == f"{name}-2"
    assert await store.get(ds.datasource_id) == moved
    assert (await store.update(ds.datasource_id, name=f"{name}-2")).name == f"{name}-2"
    with pytest.raises(DuplicateName):
        await store.update(ds.datasource_id, name=taken.name)
    assert await store.update(str(uuid.uuid4()), name="x") is None
    assert await store.update("not-a-uuid", name="x") is None
