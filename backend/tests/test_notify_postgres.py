"""Notifications, webhooks and account emails on real Postgres. Runs when
AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.auth import AuthService, PostgresAuthStore
from aep.notify import Notice, PostgresNotificationStore, Webhook

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    yield pool
    await pool.close()


def notice(title: str, run_id: str = "r1", at: datetime | None = None) -> Notice:
    when = at or datetime.now(UTC)
    return Notice("run.done", run_id, "hello", "问答示例", title, "", f"/runs/{run_id}", when)


async def test_notices_per_account(pool):
    store = PostgresNotificationStore(pool)
    await store.setup()
    await store.setup()  # idempotent
    me, other = f"u_{uuid.uuid4().hex[:8]}", f"u_{uuid.uuid4().hex[:8]}"
    # Notices to nobody in particular are everyone's, so count from what is there.
    _, shared = await store.page(me, 1, None, False)
    await store.add(notice("一"), [me, other])
    await store.add(notice("二"), [me])
    items, unread = await store.page(me, 10, None, False)
    mine = [i for i in items if i["title"] in ("一", "二")]
    assert [i["title"] for i in mine] == ["二", "一"] and unread == shared + 2
    older, _ = await store.page(me, 10, mine[0]["id"], False)
    assert "二" not in [i["title"] for i in older]
    # Marking someone else's notice does nothing.
    others, _ = await store.page(other, 10, None, False)
    theirs = next(i["id"] for i in others if i["title"] == "一")
    assert await store.mark_read(me, [theirs], datetime.now(UTC)) == shared + 2
    assert await store.mark_read(me, [mine[0]["id"]], datetime.now(UTC)) == shared + 1
    unread_only, _ = await store.page(me, 10, None, True)
    assert "二" not in [i["title"] for i in unread_only]
    assert await store.mark_read(me, None, datetime.now(UTC)) == 0
    _, left = await store.page(other, 10, None, False)
    assert left >= 1


async def test_prune_and_webhooks(pool):
    store = PostgresNotificationStore(pool)
    await store.setup()
    run = uuid.uuid4().hex
    me = f"u_{uuid.uuid4().hex[:8]}"
    await store.add(notice("旧的", run, datetime.now(UTC) - timedelta(days=100)), [me])
    await store.prune(datetime.now(UTC) - timedelta(days=90))
    items, _ = await store.page(me, 10, None, False)
    assert not [i for i in items if i["run_id"] == run]

    hook = Webhook(
        f"wh_{uuid.uuid4().hex[:8]}",
        "群",
        "wecom",
        "https://qyapi.weixin.qq.com/send?key=k",
        ["run.done"],
        None,
    )
    await store.save_webhook(hook)
    hook.apps, hook.last_status, hook.last_error = ["hello"], "error", "失败"
    await store.save_webhook(hook)
    found = await store.webhook(hook.webhook_id)
    assert found is not None and found.apps == ["hello"] and found.last_error == "失败"
    assert hook.webhook_id in [w.webhook_id for w in await store.webhooks()]
    assert await store.delete_webhook(hook.webhook_id)
    assert not await store.delete_webhook(hook.webhook_id)
    assert await store.webhook(hook.webhook_id) is None


async def test_account_email(pool):
    auth = AuthService(PostgresAuthStore(pool), enabled=True)
    await auth.setup()
    user = await auth.create(
        f"u{uuid.uuid4().hex[:10]}", "李雷", ["user"], "correct-horse", "lilei@example.com"
    )
    assert (await auth.store.get_user(user.user_id)).email == "lilei@example.com"
    await auth.update(user.user_id, email="")
    assert (await auth.store.get_user(user.user_id)).email is None
