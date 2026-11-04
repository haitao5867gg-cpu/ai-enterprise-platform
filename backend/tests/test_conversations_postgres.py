"""Conversations on real Postgres (docs/api/conversations.md).

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep import conversations as chat
from aep.conversations import Conversation, PostgresConversationStore
from aep.datasources import MemoryDataSourceStore
from aep.runtime.catalog import AppCatalog
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import ConversationBusy, RunService
from aep.runtime.store import PostgresRunStore, TurnTaken
from tests.conftest import SETTINGS, FakeGateway
from tests.test_conversations import form_chat, llm_chat

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@asynccontextmanager
async def platform(gateway: FakeGateway) -> AsyncIterator[RunService]:
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        store = PostgresRunStore(pool)
        await store.setup()
        conversations = PostgresConversationStore(pool)
        await conversations.setup()
        yield RunService(
            store,
            checkpointer,
            default_apps(),
            AppContext(llm_client=gateway.client, settings=SETTINGS),
            conversations=conversations,
        )
    finally:
        await pool.close()


async def publish(service: RunService, app: str, config: dict) -> None:
    catalog = AppCatalog(service, MemoryDataSourceStore(), SETTINGS)
    await catalog.create(app, app, "", {"blank": True}, None)
    await catalog.save_draft(app, config, None)
    await catalog.publish(app, "测试", None)


async def test_the_store_keeps_conversations_and_replies():
    async with platform(FakeGateway()) as service:
        store = service.conversations
        owner = chat.new_id()  # an owner no other test run has
        made = await store.create(
            Conversation(
                conversation_id=chat.new_id(),
                app="chat",
                title=None,
                draft=False,
                input={"datasource": "ds_1"},
                owner=owner,
                created_by="李雷",
                project={"project_id": "p1", "environment": "dev", "params": {}},
            )
        )
        got = await store.get(made.conversation_id)
        assert got is not None and got.input == {"datasource": "ds_1"}
        assert got.project == {"project_id": "p1", "environment": "dev", "params": {}}
        assert got.shown_title == "新对话"
        assert await store.titles([made.conversation_id, "conv_nope"]) == {
            made.conversation_id: "新对话"
        }
        renamed = await store.update(made.conversation_id, title="住宿标准")
        assert renamed is not None and renamed.title == "住宿标准"
        long_ago = datetime.now(UTC) - timedelta(days=40)
        moved = await store.update(made.conversation_id, updated_at=long_ago)
        assert moved is not None and moved.title == "住宿标准" and moved.updated_at == long_ago
        assert [c.conversation_id for c in await store.owned(owner, "chat", False, 10)] == [
            made.conversation_id
        ]
        assert await store.owned(owner, "chat", True, 10) == []
        assert await store.owned(owner, "other", False, 10) == []
        before = datetime.now(UTC) - timedelta(days=30)
        assert made.conversation_id in {c.conversation_id for c in await store.expired(before)}

        run_id = "00000000-0000-4000-8000-0000000000c1"
        said = chat.Reply(
            run_id, made.conversation_id, "邮箱 a", "邮箱 ‹t›", {"‹t›": "a"}, "masked"
        )
        await store.save_reply(said)
        await store.save_reply(said)  # saved again (a retried turn): still one
        assert await store.replies(made.conversation_id) == {run_id: said}
        await store.delete(made.conversation_id)
        assert await store.get(made.conversation_id) is None
        assert await store.replies(made.conversation_id) == {}


async def test_turns_on_postgres():
    gateway = FakeGateway(["一线城市 600 元", "二线城市 450 元"])
    async with platform(gateway) as service:
        app = f"chat-{chat.new_id()[5:13]}"
        await publish(service, app, llm_chat())
        found = await service.open_conversation(app, owner="", created_by="李雷")
        first = await service.send(found, "出差住宿标准是多少？", "李雷")
        await service.wait_idle()
        found = await service.conversations.get(found.conversation_id)
        assert found is not None and found.title == "出差住宿标准是多少？"
        second = await service.send(found, "那二线城市呢？", "李雷")
        await service.wait_idle()
        assert (second.conversation_id, second.conversation_turn) == (found.conversation_id, 2)
        sent = gateway.requests[1]["body"]["messages"]
        assert [m["content"] for m in sent if m["role"] != "system"] == [
            "出差住宿标准是多少？",
            "一线城市 600 元",
            "那二线城市呢？",
        ]
        turns = await service.turns(found.conversation_id)
        assert [(v.record.conversation_turn, said) for v, said in turns] == [
            (1, "一线城市 600 元"),
            (2, "二线城市 450 元"),
        ]
        # A turn number is taken once, whichever process takes it.
        with pytest.raises(TurnTaken):
            await service._store.create(  # noqa: SLF001
                app, {}, conversation_id=found.conversation_id, conversation_turn=2
            )

        long_ago = datetime.now(UTC) - timedelta(days=31)
        await service.conversations.update(found.conversation_id, updated_at=long_ago)
        assert await service.clear_expired(30) >= 1
        assert await service.conversations.get(found.conversation_id) is None
        cleared = (await service.get(first.run_id)).record
        assert cleared.status == "done" and cleared.cleared_at is not None
        assert cleared.input == {} and cleared.error is None
        assert (await service.get(first.run_id)).result is None
        assert await service._store.in_conversation(found.conversation_id) != []  # noqa: SLF001


async def test_a_waiting_turn_holds_the_next_on_postgres():
    async with platform(FakeGateway()) as service:
        app = f"orders-{chat.new_id()[5:13]}"
        await publish(service, app, form_chat())
        found = await service.open_conversation(app, owner="", created_by=None)
        await service.send(found, "补录一单", None)
        await service.wait_idle()
        with pytest.raises(ConversationBusy):
            await service.send(found, "再补一单", None)
        await service.delete_conversation(found.conversation_id)
        turns = await service._store.in_conversation(found.conversation_id)  # noqa: SLF001
        assert [(r.status, r.cleared_at is not None) for r in turns] == [("cancelled", True)]
