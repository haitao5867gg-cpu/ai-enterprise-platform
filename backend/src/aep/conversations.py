"""Conversations: one app's runs strung into a chat (docs/api/conversations.md).

Each message a person sends starts an ordinary run of the app: a turn. What the turns
before it said goes to the models of the new turn as its history: only each done turn's
message and reply, never the tools' results in between, the last ``history_turns`` of
them and at most ``HISTORY_CHARS`` characters, whole turns dropped from the oldest.

The data outbound policy (aep.policy) holds for the history as for anything else bound
for a model. A reply people see has the run's tokens turned back into data values; the
history carries it as the model wrote it (``Reply.for_model``), and a turn that ran at a
looser samples level than the new one is left out. Tokens are worked out from the
conversation's id instead of the run's (``mask_key``), and the tokens earlier turns
turned back come along, so a token the model repeats from the history still turns back.
"""

import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.policy import LEVELS, TOKEN
from aep.runtime.store import DEFAULT_TENANT, RunRecord

MAX_MESSAGE = 8000
MAX_TITLE = 80
# A conversation nobody named is named after the start of its first message.
TITLE_CHARS = 30
UNTITLED = "新对话"
DEFAULT_HISTORY_TURNS = 10
MAX_HISTORY_TURNS = 50
HISTORY_CHARS = 20000
# The start input a turn's message goes into.
MESSAGE = "message"


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    app: str
    # None until someone names it or its first message does.
    title: str | None
    draft: bool
    # The start inputs other than the message, the same for every turn.
    input: dict[str, Any]
    # Whose it is: a user id with sign-in on, "" with it off, "key:<key_id>" for a
    # conversation an API key opened (docs/api/app-access.md, "会话").
    owner: str
    created_by: str | None
    # The project choice every turn binds (as POST /runs takes it), or None.
    project: dict[str, Any] | None = None
    tenant_id: str = DEFAULT_TENANT
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def shown_title(self) -> str:
        return self.title or UNTITLED


@dataclass(frozen=True)
class Reply:
    """What a done turn said, kept for the turns after it: the reply as people see it
    (``text``), as the model wrote it (``for_model``: tokens of masked data kept), the
    tokens the run turned back into values, and the samples level it ran at."""

    run_id: str
    conversation_id: str
    text: str | None
    for_model: str | None
    restored: dict[str, str]
    samples: str


def new_id() -> str:
    return "conv_" + secrets.token_hex(12)


def mask_key(state: dict[str, Any]) -> str:
    """What a run's tokens are worked out from: its conversation's id in a conversation
    (a token stays the same from turn to turn), else the run's id."""
    conversation = state.get("conversation")
    if isinstance(conversation, dict) and isinstance(conversation.get("id"), str):
        return conversation["id"]
    return str(state.get("run_id") or "")


def chat_settings(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """``history_turns`` and ``reply`` (the result field that is a turn's reply) of an
    app configuration that converses, else None."""
    found = (config or {}).get("conversation")
    if not isinstance(found, dict) or found.get("enabled", True) is not True:
        return None
    turns = found.get("history_turns", DEFAULT_HISTORY_TURNS)
    reply = found.get("reply")
    return {
        "history_turns": turns if isinstance(turns, int) else DEFAULT_HISTORY_TURNS,
        "reply": reply if isinstance(reply, str) else "reply",
    }


def reply_text(config: dict[str, Any] | None, result: Any) -> str | None:
    """A finished turn's reply: the result field the app names, when it is text."""
    settings = chat_settings(config)
    if settings is None or not isinstance(result, dict):
        return None
    value = result.get(settings["reply"])
    return value if isinstance(value, str) and value.strip() else None


def short_title(message: str) -> str:
    line = " ".join(message.split())
    return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS]


def history(
    turns: Iterable[tuple[RunRecord, Reply | None]], samples: str, limit: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The history a new turn gets from the turns before it (oldest first), at the new
    turn's samples level: ``[{"message", "reply"}]``, and the tokens those turns turned
    back into values (token: text)."""
    level = LEVELS.index(samples) if samples in LEVELS else 0
    kept: list[tuple[dict[str, Any], dict[str, str]]] = []
    for run, said in turns:
        message = run.input.get(MESSAGE)
        if run.status != "done" or run.cleared_at is not None or not isinstance(message, str):
            continue
        reply: str | None = None
        restored: dict[str, str] = {}
        if said is not None:
            then = LEVELS.index(said.samples) if said.samples in LEVELS else 0
            # Values the model of that turn saw, which this turn's may not.
            if then < level:
                continue
            reply = said.text if samples == "full" else said.for_model
            restored = said.restored
        kept.append(({"message": message, "reply": reply}, restored))
    kept = kept[-limit:] if limit > 0 else []
    while kept and sum(_chars(turn) for turn, _ in kept) > HISTORY_CHARS:
        kept.pop(0)
    tokens: dict[str, str] = {}
    for _, restored in kept:
        tokens |= restored
    return [turn for turn, _ in kept], tokens


def _chars(turn: dict[str, Any]) -> int:
    return len(turn["message"]) + len(turn["reply"] or "")


def history_chars(turns: list[dict[str, Any]]) -> int:
    """How many characters the turns' messages and replies have together."""
    return sum(_chars(turn) for turn in turns)


def tokens_in(turns: list[dict[str, Any]]) -> set[str]:
    """The masking tokens the turns' messages and replies hold."""
    return {
        m.group(0)
        for turn in turns
        for text in (turn["message"], turn["reply"] or "")
        for m in TOKEN.finditer(text)
    }


def as_text(turns: list[dict[str, Any]]) -> str:
    """The history as ``{{conversation.history}}`` puts it into a prompt."""
    lines = []
    for turn in turns:
        lines.append(f"用户：{turn['message']}")
        if turn.get("reply"):
            lines.append(f"助手：{turn['reply']}")
    return "\n".join(lines)


def as_messages(turns: list[dict[str, Any]], answer: Any = None) -> list[dict[str, str]]:
    """The history as chat messages: each turn's message and reply; ``answer`` writes a
    reply the way the node asks its model to answer (an agent's JSON)."""
    messages: list[dict[str, str]] = []
    for turn in turns:
        messages.append({"role": "user", "content": turn["message"]})
        if turn.get("reply"):
            reply = answer(turn["reply"]) if answer else turn["reply"]
            messages.append({"role": "assistant", "content": reply})
    return messages


# Storage


class ConversationStore(Protocol):
    async def setup(self) -> None: ...
    async def create(self, conversation: Conversation) -> Conversation: ...
    async def get(self, conversation_id: str) -> Conversation | None: ...
    async def owned(
        self, owner: str, app: str | None, draft: bool, limit: int
    ) -> list[Conversation]:
        """The owner's conversations, the latest turn first."""
        ...

    async def update(
        self, conversation_id: str, *, title: str | None = None, updated_at: datetime | None = None
    ) -> Conversation | None: ...
    async def delete(self, conversation_id: str) -> None:
        """The conversation and its turns' replies."""
        ...

    async def expired(self, before: datetime) -> list[Conversation]:
        """Conversations whose latest turn started before ``before``."""
        ...

    async def titles(self, conversation_ids: list[str]) -> dict[str, str]: ...
    async def save_reply(self, reply: Reply) -> None: ...
    async def replies(self, conversation_id: str) -> dict[str, Reply]:
        """Each done turn's reply, by run id."""
        ...


class MemoryConversationStore:
    def __init__(self) -> None:
        self.conversations: dict[str, Conversation] = {}
        self._replies: dict[str, Reply] = {}

    async def setup(self) -> None:
        return None

    async def create(self, conversation: Conversation) -> Conversation:
        self.conversations[conversation.conversation_id] = conversation
        return conversation

    async def get(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)

    async def owned(
        self, owner: str, app: str | None, draft: bool, limit: int
    ) -> list[Conversation]:
        found = [
            c
            for c in self.conversations.values()
            if c.owner == owner and c.draft == draft and (app is None or c.app == app)
        ]
        found.sort(key=lambda c: c.updated_at, reverse=True)
        return found[:limit]

    async def update(
        self, conversation_id: str, *, title: str | None = None, updated_at: datetime | None = None
    ) -> Conversation | None:
        found = self.conversations.get(conversation_id)
        if found is None:
            return None
        changed = replace(
            found,
            title=title if title is not None else found.title,
            updated_at=updated_at or found.updated_at,
        )
        self.conversations[conversation_id] = changed
        return changed

    async def delete(self, conversation_id: str) -> None:
        self.conversations.pop(conversation_id, None)
        for run_id in [k for k, r in self._replies.items() if r.conversation_id == conversation_id]:
            del self._replies[run_id]

    async def expired(self, before: datetime) -> list[Conversation]:
        return [c for c in self.conversations.values() if c.updated_at < before]

    async def titles(self, conversation_ids: list[str]) -> dict[str, str]:
        return {
            key: self.conversations[key].shown_title
            for key in conversation_ids
            if key in self.conversations
        }

    async def save_reply(self, reply: Reply) -> None:
        self._replies[reply.run_id] = reply

    async def replies(self, conversation_id: str) -> dict[str, Reply]:
        return {k: r for k, r in self._replies.items() if r.conversation_id == conversation_id}


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id text PRIMARY KEY,
    tenant_id       text NOT NULL DEFAULT 'default',
    app             text NOT NULL,
    title           text,
    draft           boolean NOT NULL DEFAULT false,
    input           jsonb NOT NULL DEFAULT '{}',
    owner           text NOT NULL,
    created_by      text,
    project         jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
)""",
    "CREATE INDEX IF NOT EXISTS conversations_owner_idx"
    " ON conversations (tenant_id, owner, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS conversations_updated_idx ON conversations (updated_at)",
    """
CREATE TABLE IF NOT EXISTS conversation_replies (
    run_id          uuid PRIMARY KEY,
    tenant_id       text NOT NULL DEFAULT 'default',
    conversation_id text NOT NULL,
    text            text,
    for_model       text,
    restored        jsonb NOT NULL DEFAULT '{}',
    samples         text NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS conversation_replies_conversation_idx"
    " ON conversation_replies (conversation_id)",
]

COLUMNS = (
    "conversation_id, app, title, draft, input, owner, created_by, project, tenant_id,"
    " created_at, updated_at"
)


class PostgresConversationStore:
    """Conversations in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def create(self, conversation: Conversation) -> Conversation:
        c = conversation
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO conversations (conversation_id, app, title, draft, input, owner,"
                " created_by, project, tenant_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {COLUMNS}",
                (
                    c.conversation_id,
                    c.app,
                    c.title,
                    c.draft,
                    Jsonb(c.input),
                    c.owner,
                    c.created_by,
                    Jsonb(c.project) if c.project is not None else None,
                    c.tenant_id,
                ),
            )
            return Conversation(**await cur.fetchone())

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = await cur.fetchone()
            return Conversation(**row) if row else None

    async def owned(
        self, owner: str, app: str | None, draft: bool, limit: int
    ) -> list[Conversation]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM conversations WHERE owner = %s AND draft = %s"
                " AND (%s::text IS NULL OR app = %s) ORDER BY updated_at DESC LIMIT %s",
                (owner, draft, app, app, limit),
            )
            return [Conversation(**row) for row in await cur.fetchall()]

    async def update(
        self, conversation_id: str, *, title: str | None = None, updated_at: datetime | None = None
    ) -> Conversation | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE conversations SET title = coalesce(%s, title),"
                " updated_at = coalesce(%s, updated_at) WHERE conversation_id = %s"
                f" RETURNING {COLUMNS}",
                (title, updated_at, conversation_id),
            )
            row = await cur.fetchone()
            return Conversation(**row) if row else None

    async def delete(self, conversation_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM conversation_replies WHERE conversation_id = %s", (conversation_id,)
            )
            await conn.execute(
                "DELETE FROM conversations WHERE conversation_id = %s", (conversation_id,)
            )

    async def expired(self, before: datetime) -> list[Conversation]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM conversations WHERE updated_at < %s", (before,)
            )
            return [Conversation(**row) for row in await cur.fetchall()]

    async def titles(self, conversation_ids: list[str]) -> dict[str, str]:
        if not conversation_ids:
            return {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT conversation_id, title FROM conversations WHERE conversation_id = ANY(%s)",
                (conversation_ids,),
            )
            return {
                row["conversation_id"]: row["title"] or UNTITLED for row in await cur.fetchall()
            }

    async def save_reply(self, reply: Reply) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO conversation_replies (run_id, conversation_id, text, for_model,"
                " restored, samples) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (run_id) DO UPDATE SET text = excluded.text,"
                " for_model = excluded.for_model, restored = excluded.restored,"
                " samples = excluded.samples",
                (
                    uuid.UUID(reply.run_id),
                    reply.conversation_id,
                    reply.text,
                    reply.for_model,
                    Jsonb(reply.restored),
                    reply.samples,
                ),
            )

    async def replies(self, conversation_id: str) -> dict[str, Reply]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id::text, conversation_id, text, for_model, restored, samples"
                " FROM conversation_replies WHERE conversation_id = %s",
                (conversation_id,),
            )
            return {row["run_id"]: Reply(**row) for row in await cur.fetchall()}
