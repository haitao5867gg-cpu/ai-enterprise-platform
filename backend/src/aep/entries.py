"""The ways into an app, each of which can be closed (docs/api/app-access.md, "入口"):
the platform's use page and run pages (``use``), the app API (``api``), MCP (``mcp``)
and being called by other apps (``apps``). Switches are app settings, not part of a
version: they take effect at once and stay through publishing and rolling back.

Every run also records which way it came in (``RunRecord.entry``) for the app's usage
page (docs/api/ops.md, "应用的使用情况"); runs from before that are told by how they
were started (:func:`of`).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import DEFAULT_TENANT, RunRecord

ENTRIES = ("use", "api", "mcp", "apps")
# How a run came in: an entry, a schedule, or something else (an eval, a rerun from a step).
RUN_ENTRIES = (*ENTRIES, "schedule", "other")
EVAL_ACTOR = "aep-eval"
API_PREFIX = "API："

CLOSED_TO_USERS = "这个应用没有开放给使用者"
CLOSED_ENTRY = "这个应用没有开放这个入口"


def not_callable(title: str) -> str:
    return f"应用「{title}」不允许被其他应用调用"


@dataclass
class Entries:
    app: str
    use: bool = True
    api: bool = True
    mcp: bool = True
    apps: bool = True
    updated_by: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def open(self, entry: str) -> bool:
        return bool(getattr(self, entry))


class EntryStore(Protocol):
    async def setup(self) -> None: ...
    async def get(self, app: str) -> Entries: ...
    async def put(self, value: Entries) -> None: ...


class MemoryEntryStore:
    def __init__(self) -> None:
        self.entries: dict[str, Entries] = {}

    async def setup(self) -> None:
        pass

    async def get(self, app: str) -> Entries:
        found = self.entries.get(app)
        return Entries(**vars(found)) if found else Entries(app)

    async def put(self, value: Entries) -> None:
        self.entries[value.app] = Entries(**vars(value))


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_entries (
    tenant_id   text NOT NULL DEFAULT 'default',
    app         text NOT NULL,
    use         boolean NOT NULL DEFAULT true,
    api         boolean NOT NULL DEFAULT true,
    mcp         boolean NOT NULL DEFAULT true,
    apps        boolean NOT NULL DEFAULT true,
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app)
)"""


class PostgresEntryStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def get(self, app: str) -> Entries:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT app, use, api, mcp, apps, updated_by, updated_at FROM app_entries"
                " WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            row = await cur.fetchone()
        return Entries(**row) if row else Entries(app)

    async def put(self, value: Entries) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO app_entries"
                " (tenant_id, app, use, api, mcp, apps, updated_by, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, app) DO UPDATE SET use = EXCLUDED.use,"
                " api = EXCLUDED.api, mcp = EXCLUDED.mcp, apps = EXCLUDED.apps,"
                " updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at",
                (
                    DEFAULT_TENANT,
                    value.app,
                    value.use,
                    value.api,
                    value.mcp,
                    value.apps,
                    value.updated_by,
                    value.updated_at,
                ),
            )


def started(trigger: dict[str, Any] | None, start_at: dict[str, Any] | None) -> str | None:
    """The entry a new run takes from how it was started, when its starter names none."""
    kind = (trigger or {}).get("kind")
    if kind == "schedule":
        return "schedule"
    if kind == "app":
        return "apps"
    if start_at:
        return "other"
    return None


def of(record: RunRecord) -> str:
    """How the run came in: as it was recorded, or for a run from before entries were
    recorded, told by how it was started (API and MCP runs both count as ``api``)."""
    if record.entry in RUN_ENTRIES:
        return record.entry
    found = started(record.trigger, record.start_at)
    if found:
        return found
    if record.created_by == EVAL_ACTOR:
        return "other"
    if (record.created_by or "").startswith(API_PREFIX):
        return "api"
    return "use"
