"""Flow apps: which exist, their names, and which are archived.

Their configurations are app versions like any app's (``aep.runtime.versions``);
this store only knows the apps themselves. An archived app keeps its id, its
versions and its runs, but is no longer listed or started.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import DEFAULT_TENANT


class AppExists(Exception):
    pass


@dataclass(frozen=True)
class FlowApp:
    app: str
    title: str
    summary: str = ""
    created_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    archived_at: datetime | None = None


class FlowAppStore(Protocol):
    async def setup(self) -> None: ...
    async def ensure(self, app: str, title: str, summary: str) -> None: ...
    async def create(self, app: str, title: str, summary: str, actor: str | None) -> FlowApp: ...
    async def get(self, app: str) -> FlowApp | None: ...
    async def all(self) -> list[FlowApp]: ...
    async def update(self, app: str, title: str | None, summary: str | None) -> None: ...
    async def archive(self, app: str) -> None: ...


class MemoryFlowAppStore:
    """In-process store for tests."""

    def __init__(self) -> None:
        self._apps: dict[str, FlowApp] = {}

    async def setup(self) -> None:
        return None

    async def ensure(self, app: str, title: str, summary: str) -> None:
        self._apps.setdefault(app, FlowApp(app, title, summary))

    async def create(self, app: str, title: str, summary: str, actor: str | None) -> FlowApp:
        if app in self._apps:
            raise AppExists(app)
        self._apps[app] = FlowApp(app, title, summary, actor)
        return self._apps[app]

    async def get(self, app: str) -> FlowApp | None:
        """The app, archived or not."""
        return self._apps.get(app)

    async def all(self) -> list[FlowApp]:
        """The apps that are not archived."""
        return [a for a in self._apps.values() if a.archived_at is None]

    async def update(self, app: str, title: str | None, summary: str | None) -> None:
        old = self._apps[app]
        self._apps[app] = replace(
            old,
            title=old.title if title is None else title,
            summary=old.summary if summary is None else summary,
        )

    async def archive(self, app: str) -> None:
        self._apps[app] = replace(self._apps[app], archived_at=datetime.now(UTC))


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS apps (
    tenant_id   text NOT NULL DEFAULT 'default',
    app         text NOT NULL,
    title       text NOT NULL,
    summary     text NOT NULL DEFAULT '',
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz,
    PRIMARY KEY (tenant_id, app)
)""",
]

COLUMNS = "app, title, summary, created_by, created_at, archived_at"


class PostgresFlowAppStore:
    """Flow apps in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def ensure(self, app: str, title: str, summary: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO apps (tenant_id, app, title, summary) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (DEFAULT_TENANT, app, title, summary),
            )

    async def create(self, app: str, title: str, summary: str, actor: str | None) -> FlowApp:
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "INSERT INTO apps (tenant_id, app, title, summary, created_by)"
                    f" VALUES (%s, %s, %s, %s, %s) RETURNING {COLUMNS}",
                    (DEFAULT_TENANT, app, title, summary, actor),
                )
            except UniqueViolation as exc:
                raise AppExists(app) from exc
            return FlowApp(**await cur.fetchone())

    async def get(self, app: str) -> FlowApp | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM apps WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            row = await cur.fetchone()
            return FlowApp(**row) if row else None

    async def all(self) -> list[FlowApp]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM apps WHERE tenant_id = %s AND archived_at IS NULL"
                " ORDER BY app",
                (DEFAULT_TENANT,),
            )
            return [FlowApp(**row) for row in await cur.fetchall()]

    async def update(self, app: str, title: str | None, summary: str | None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE apps SET title = coalesce(%s, title), summary = coalesce(%s, summary)"
                " WHERE tenant_id = %s AND app = %s",
                (title, summary, DEFAULT_TENANT, app),
            )

    async def archive(self, app: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE apps SET archived_at = now() WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
