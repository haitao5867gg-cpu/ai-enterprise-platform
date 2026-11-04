"""Apps for other systems: API keys and what a run looks like from outside
(docs/api/app-access.md). The app API (``aep.api.app_access``) and the MCP endpoint
(``aep.app_mcp``) both use it; apps themselves know nothing of it.
"""

import asyncio
import hashlib
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from aep.conversations import MAX_MESSAGE, MESSAGE
from aep.entries import CLOSED_ENTRY
from aep.runtime.service import ConversationGone, InvalidValues, RunService, RunView
from aep.runtime.store import DEFAULT_TENANT, RunRecord

PREFIX = "aep_"
SHOWN = 12  # characters of a key shown in lists
KEYS_PER_APP = 20
MAX_WAIT = 60
# How often a key's last use is written down at most.
TOUCH_EVERY = timedelta(minutes=1)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class ApiKey:
    key_id: str
    app: str
    name: str
    prefix: str
    digest: str
    created_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None

    def out(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "app": self.app,
            "name": self.name,
            "prefix": self.prefix,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class ApiKeyStore(Protocol):
    async def setup(self) -> None: ...
    async def list(self, app: str) -> list[ApiKey]: ...
    async def add(self, key: ApiKey) -> None: ...
    async def find(self, digest: str) -> ApiKey | None: ...
    async def delete(self, app: str, key_id: str) -> bool: ...
    async def touch(self, key_id: str, at: datetime) -> None: ...


class MemoryApiKeyStore:
    def __init__(self) -> None:
        self.keys: dict[str, ApiKey] = {}

    async def setup(self) -> None:
        pass

    async def list(self, app: str) -> list[ApiKey]:
        found = [replace(k) for k in self.keys.values() if k.app == app]
        return sorted(found, key=lambda k: k.created_at, reverse=True)

    async def add(self, key: ApiKey) -> None:
        self.keys[key.key_id] = replace(key)

    async def find(self, digest: str) -> ApiKey | None:
        found = next((k for k in self.keys.values() if k.digest == digest), None)
        return replace(found) if found else None

    async def delete(self, app: str, key_id: str) -> bool:
        key = self.keys.get(key_id)
        if key is None or key.app != app:
            return False
        del self.keys[key_id]
        return True

    async def touch(self, key_id: str, at: datetime) -> None:
        if key_id in self.keys:
            self.keys[key_id].last_used_at = at


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id        text PRIMARY KEY,
    tenant_id     text NOT NULL DEFAULT 'default',
    app           text NOT NULL,
    name          text NOT NULL,
    prefix        text NOT NULL,
    digest        text NOT NULL UNIQUE,
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz
)""",
    "CREATE INDEX IF NOT EXISTS api_keys_app ON api_keys (tenant_id, app)",
]
_COLUMNS = "key_id, app, name, prefix, digest, created_by, created_at, last_used_at"


def _key(row: dict[str, Any]) -> ApiKey:
    return ApiKey(**{k: row[k] for k in _COLUMNS.split(", ")})


class PostgresApiKeyStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def list(self, app: str) -> list[ApiKey]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM api_keys WHERE tenant_id = %s AND app = %s"
                " ORDER BY created_at DESC",
                (DEFAULT_TENANT, app),
            )
            return [_key(r) for r in await cur.fetchall()]

    async def add(self, key: ApiKey) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO api_keys (tenant_id, {_COLUMNS})"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    DEFAULT_TENANT,
                    key.key_id,
                    key.app,
                    key.name,
                    key.prefix,
                    key.digest,
                    key.created_by,
                    key.created_at,
                    key.last_used_at,
                ),
            )

    async def find(self, digest: str) -> ApiKey | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM api_keys WHERE tenant_id = %s AND digest = %s",
                (DEFAULT_TENANT, digest),
            )
            row = await cur.fetchone()
            return _key(row) if row else None

    async def delete(self, app: str, key_id: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM api_keys WHERE tenant_id = %s AND app = %s AND key_id = %s",
                (DEFAULT_TENANT, app, key_id),
            )
            return cur.rowcount > 0

    async def touch(self, key_id: str, at: datetime) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE api_keys SET last_used_at = %s WHERE tenant_id = %s AND key_id = %s",
                (at, DEFAULT_TENANT, key_id),
            )


class TooManyKeys(Exception):
    pass


class ApiKeys:
    def __init__(self, store: ApiKeyStore) -> None:
        self.store = store

    async def create(self, app: str, name: str, by: str | None) -> tuple[ApiKey, str]:
        if len(await self.store.list(app)) >= KEYS_PER_APP:
            raise TooManyKeys(app)
        secret = PREFIX + secrets.token_urlsafe(32)
        key = ApiKey("key_" + secrets.token_hex(6), app, name, secret[:SHOWN], digest(secret), by)
        await self.store.add(key)
        return key, secret

    async def check(self, secret: str | None) -> ApiKey | None:
        """The key with this secret, or None; notes when it was last used."""
        if not secret or not secret.startswith(PREFIX):
            return None
        key = await self.store.find(digest(secret))
        if key is None:
            return None
        now = datetime.now(UTC)
        if key.last_used_at is None or now - key.last_used_at >= TOUCH_EVERY:
            await self.store.touch(key.key_id, now)
            key.last_used_at = now
        return key


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None


class ConversationProblem(Exception):
    """A conversation request the app API refuses: its status code and why."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def key_owner(key: ApiKey) -> str:
    """Whose a conversation an API key opens is: only that key's (docs/api/app-access.md)."""
    return f"key:{key.key_id}"


async def start(
    service: RunService,
    key: ApiKey,
    input: dict[str, Any],
    *,
    project: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    new_conversation: bool = False,
    entry: str = "api",
) -> RunRecord:
    """A run of the key's app: on its own, as the first turn of a new conversation, or
    as the next turn of one the key opened (docs/api/app-access.md, "会话"). Raises what
    ``RunService.create``, ``open_conversation`` and ``send`` raise, and
    ``ConversationProblem``. ``entry`` is the way it came in: ``api`` or ``mcp``."""
    if not (await service.entries.get(key.app)).open(entry):
        raise ConversationProblem(403, CLOSED_ENTRY)
    requester = f"API：{key.name}"
    if conversation_id is not None and new_conversation:
        raise ConversationProblem(422, "new_conversation 和 conversation_id 只能给一个")
    if conversation_id is None and not new_conversation:
        return await service.create(
            key.app, input, requester=requester, project=project, entry=entry
        )
    given = dict(input)
    message = given.pop(MESSAGE, None)
    if not isinstance(message, str) or not message.strip():
        raise InvalidValues({MESSAGE: "消息不能为空"})
    if len(message) > MAX_MESSAGE:
        raise InvalidValues({MESSAGE: f"消息最多 {MAX_MESSAGE} 个字符"})
    if conversation_id is not None:
        found = await service.conversations.get(conversation_id)
        if found is None or found.owner != key_owner(key) or found.app != key.app:
            raise ConversationProblem(404, "没有找到这个会话")
        if given or project is not None:
            raise ConversationProblem(422, "会话里的其他输入在开会话时已经定了")
    else:
        found = await service.open_conversation(
            key.app, owner=key_owner(key), created_by=requester, input=given, project=project
        )
    try:
        return await service.send(found, message, requester, entry)
    except ConversationGone as exc:
        raise ConversationProblem(404, "没有找到这个会话") from exc


async def settle(service: RunService, run_id: str, wait: float) -> RunView:
    """The run once it is no longer running, or after ``wait`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, min(wait, MAX_WAIT))
    while True:
        view = await service.get(run_id)
        if view.record.status != "running" or loop.time() >= deadline:
            return view
        await asyncio.sleep(min(0.5, max(0.0, deadline - loop.time())))


def run_result(view: RunView, web_url: str) -> dict[str, Any]:
    r = view.record
    pending = view.pending
    return {
        "run_id": r.run_id,
        "app": r.app,
        "status": r.status,
        "result": view.result if r.status == "done" else None,
        "pending": {
            "kind": pending.get("kind"),
            "prompt": pending.get("prompt") or "",
            "fields": pending.get("fields") if pending.get("kind") == "form" else None,
        }
        if pending
        else None,
        "error": r.error if r.status == "error" else None,
        "link": f"{web_url.rstrip('/')}/runs/{r.run_id}",
        "conversation_id": r.conversation_id,
        "turn": r.conversation_turn,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }
