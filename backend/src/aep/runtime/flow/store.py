"""Flow apps: which exist, their names, and which are archived; and their secrets.

Their configurations are app versions like any app's (``aep.runtime.versions``);
this store only knows the apps themselves. An archived app keeps its id, its
versions and its runs, but is no longer listed or started. Secrets belong to the
app, not to a version, so they are never part of a configuration or an export.
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


@dataclass(frozen=True)
class SecretInfo:
    name: str
    updated_at: datetime


class SecretStore(Protocol):
    """An app's secrets (API keys and the like for its tools). Values are only read by
    a running tool node; nothing else gets them back."""

    async def setup(self) -> None: ...
    async def get(self, app: str, name: str) -> str | None: ...
    async def set(self, app: str, name: str, value: str) -> None: ...
    async def delete(self, app: str, name: str) -> None: ...
    async def names(self, app: str) -> list[SecretInfo]: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], tuple[str, datetime]] = {}

    async def setup(self) -> None:
        return None

    async def get(self, app: str, name: str) -> str | None:
        found = self._values.get((app, name))
        return found[0] if found else None

    async def set(self, app: str, name: str, value: str) -> None:
        self._values[(app, name)] = (value, datetime.now(UTC))

    async def delete(self, app: str, name: str) -> None:
        self._values.pop((app, name), None)

    async def names(self, app: str) -> list[SecretInfo]:
        return sorted(
            (SecretInfo(n, at) for (a, n), (_, at) in self._values.items() if a == app),
            key=lambda s: s.name,
        )


SECRET_SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS app_secrets (
    tenant_id  text NOT NULL DEFAULT 'default',
    app        text NOT NULL,
    name       text NOT NULL,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app, name)
)""",
]


class PostgresSecretStore:
    """App secrets in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SECRET_SCHEMA:
                await conn.execute(statement)

    async def get(self, app: str, name: str) -> str | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT value FROM app_secrets WHERE tenant_id = %s AND app = %s AND name = %s",
                (DEFAULT_TENANT, app, name),
            )
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set(self, app: str, name: str, value: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO app_secrets (tenant_id, app, name, value) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, app, name)"
                " DO UPDATE SET value = excluded.value, updated_at = now()",
                (DEFAULT_TENANT, app, name, value),
            )

    async def delete(self, app: str, name: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM app_secrets WHERE tenant_id = %s AND app = %s AND name = %s",
                (DEFAULT_TENANT, app, name),
            )

    async def names(self, app: str) -> list[SecretInfo]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT name, updated_at FROM app_secrets WHERE tenant_id = %s AND app = %s"
                " ORDER BY name",
                (DEFAULT_TENANT, app),
            )
            return [SecretInfo(**row) for row in await cur.fetchall()]
