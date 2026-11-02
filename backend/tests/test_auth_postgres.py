"""Accounts, sessions and the record of changes on real Postgres. Runs when
AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.auth import AuditRecord, AuthService, PostgresAuthStore, token_hash

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")
PASSWORD = "correct-horse"


@pytest.fixture
async def store():
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    store = PostgresAuthStore(pool)
    await store.setup()
    yield store
    await pool.close()


def name() -> str:
    return f"u{uuid.uuid4().hex[:10]}"


async def test_accounts_sign_in_and_sessions(store):
    auth = AuthService(store, enabled=True)
    await auth.setup()
    admin = await auth.create(name(), "管理员", ["admin"], PASSWORD)
    user = await auth.create(name(), "李雷", ["approver", "user"], PASSWORD)
    assert user.roles == ["user", "approver"]
    found = await store.get_user(user.user_id)
    assert found is not None and found.display_name == "李雷"
    assert [u.username for u in await store.list_users()].count(user.username) == 1

    signed, token = await auth.login(user.username, PASSWORD)
    assert (await auth.user_for(token)).user_id == user.user_id
    assert (await store.get_user(user.user_id)).last_login_at is not None
    # Only the token's hash is kept.
    assert await store.session_user(token, datetime.now(UTC)) is None
    assert await store.session_user(token_hash(token), datetime.now(UTC)) is not None

    await auth.update(user.user_id, display_name="李雷2", roles=["builder"])
    assert (await auth.user_for(token)).roles == ["builder"]
    await auth.update(user.user_id, disabled=True)
    assert await auth.user_for(token) is None

    await auth.reset_password(user.user_id, "another-pass")
    await auth.update(user.user_id, disabled=False)
    _, token = await auth.login(user.username, "another-pass")
    await auth.delete(user.user_id, admin)
    assert await auth.user_for(token) is None
    assert await store.get_user(user.user_id) is None


async def test_expired_sessions_are_pruned(store):
    auth = AuthService(store, enabled=True)
    user = await auth.create(name(), "韩梅梅", ["user"], PASSWORD)
    past = datetime.now(UTC) - timedelta(hours=1)
    await store.add_session("expired-" + user.user_id, user.user_id, past)
    assert await store.session_user("expired-" + user.user_id, datetime.now(UTC)) is None
    await store.prune(datetime.now(UTC), datetime.now(UTC) - timedelta(days=365))


async def test_record_and_page_through_changes(store):
    auth = AuthService(store, enabled=True)
    user = await auth.create(name(), "王搭建", ["builder"], PASSWORD)
    for app in ("a1", "a2", "a3"):
        await store.record(
            AuditRecord("PUT /apps/{app}/draft", 200, user, {"app": app}, "203.0.113.5")
        )
    page = await store.events(2, None, user.username, None)
    assert [e["target"]["app"] for e in page] == ["a3", "a2"]
    assert page[0]["label"] == "保存草稿" and page[0]["ip"] == "203.0.113.5"
    assert page[0]["user"]["display_name"] == "王搭建"
    rest = await store.events(10, page[-1]["id"], user.username, None)
    assert [e["target"]["app"] for e in rest] == ["a1"]
    assert len(await store.events(10, None, user.username, "草稿")) == 3
    assert len(await store.events(10, None, user.username, "100%_")) == 0
