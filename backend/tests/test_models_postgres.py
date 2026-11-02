"""Model call records and platform settings on real Postgres. Runs when
AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.llm import CallRecord
from aep.models import PostgresCallStore

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@pytest.fixture
async def store():
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    store = PostgresCallStore(pool)
    await store.setup()
    yield store
    await pool.close()


def call(model: str, ok: bool = True, cost: float | None = None, error: str | None = None):
    return CallRecord(model, "chat", "run-1", "draft", 10, 2, cost, ok, error, 120)


async def test_calls_are_summed_per_model(store):
    model = f"m-{uuid.uuid4().hex[:8]}"
    await store.record(call(model, cost=0.01))
    await store.record(call(model, cost=None))
    await store.record(call(model, ok=False, error="auth"))
    since = datetime.now(UTC) - timedelta(minutes=5)

    found = (await store.summary(since))[model]

    assert (found.calls, found.errors, found.prompt_tokens, found.completion_tokens) == (
        3,
        1,
        30,
        6,
    )
    assert found.cost_usd == pytest.approx(0.01)
    assert found.last_error == "auth"
    await store.record(call(model))
    assert (await store.summary(since))[model].last_error is None
    assert model not in await store.summary(datetime.now(UTC) + timedelta(minutes=1))


async def test_a_model_never_priced_has_no_cost(store):
    model = f"m-{uuid.uuid4().hex[:8]}"
    await store.record(call(model))
    assert (await store.summary(datetime.now(UTC) - timedelta(minutes=5)))[model].cost_usd is None


async def test_prune_and_settings(store):
    assert await store.prune(datetime.now(UTC) - timedelta(days=365)) >= 0
    key = f"k-{uuid.uuid4().hex[:8]}"
    assert await store.get(key) is None
    await store.set(key, {"chat": "claude"})
    await store.set(key, {"chat": "company", "embedding": "company-embed"})
    assert await store.get(key) == {"chat": "company", "embedding": "company-embed"}
