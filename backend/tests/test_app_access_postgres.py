"""API keys on real Postgres. Runs when AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.app_access import ApiKeys, PostgresApiKeyStore, digest

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


async def test_keys():
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    try:
        store = PostgresApiKeyStore(pool)
        await store.setup()
        await store.setup()
        keys = ApiKeys(store)
        app = f"app-{uuid.uuid4().hex[:8]}"
        first, _ = await keys.create(app, "一", "王搭建")
        second, secret = await keys.create(app, "二", None)
        assert [k.name for k in await store.list(app)] == ["二", "一"]
        found = await keys.check(secret)
        assert found is not None and found.key_id == second.key_id
        assert found.last_used_at is not None
        assert (await store.find(digest(secret))).last_used_at is not None
        assert await keys.check(secret + "x") is None
        assert not await store.delete("other-app", first.key_id)
        assert await store.delete(app, first.key_id)
        assert [k.name for k in await store.list(app)] == ["二"]
    finally:
        await pool.close()
