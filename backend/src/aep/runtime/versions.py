"""App versions: the configurations an app has had, which one is current, and its draft.

An app gets version 1 (its defaults, see ``aep.runtime.appconfig``) the first
time it is used. People change the draft; publishing it makes the next version
current, and activating an older version makes that one current again (a
rollback). Published versions never change. A draft carries its latest eval
(see ``aep.runtime.catalog``); saving the draft again drops it, and an eval
finishing after that is not kept (``revision`` tells them apart).
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import DEFAULT_TENANT

DEFAULT_NOTE = "平台默认配置"


class VersionNotFound(LookupError):
    pass


class NoDraft(Exception):
    pass


class DraftChanged(Exception):
    """The draft was saved again since the caller looked at it."""


@dataclass(frozen=True)
class AppVersion:
    app: str
    version: int
    config: dict[str, Any]
    note: str | None = None
    created_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    eval: dict[str, Any] | None = None


@dataclass(frozen=True)
class Draft:
    app: str
    config: dict[str, Any]
    revision: str
    created_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    eval: dict[str, Any] | None = None


class AppVersionStore(Protocol):
    async def setup(self) -> None: ...
    async def ensure(self, app: str, config: dict[str, Any]) -> None: ...
    async def current(self, app: str) -> int: ...
    async def versions(self, app: str) -> list[AppVersion]: ...
    async def version(self, app: str, version: int) -> AppVersion | None: ...
    async def draft(self, app: str) -> Draft | None: ...
    async def save_draft(self, app: str, config: dict[str, Any], actor: str | None) -> Draft: ...
    async def discard_draft(self, app: str) -> None: ...
    async def set_draft_eval(self, app: str, revision: str, eval: dict[str, Any]) -> bool: ...
    async def publish(
        self, app: str, note: str, actor: str | None, revision: str | None = None
    ) -> AppVersion: ...
    async def activate(self, app: str, version: int) -> None: ...


class MemoryAppVersionStore:
    """In-process store for tests."""

    def __init__(self) -> None:
        self._versions: dict[str, list[AppVersion]] = {}
        self._current: dict[str, int] = {}
        self._drafts: dict[str, Draft] = {}

    async def setup(self) -> None:
        return None

    async def ensure(self, app: str, config: dict[str, Any]) -> None:
        if app not in self._versions:
            self._versions[app] = [AppVersion(app, 1, config, DEFAULT_NOTE)]
            self._current[app] = 1

    async def current(self, app: str) -> int:
        return self._current[app]

    async def versions(self, app: str) -> list[AppVersion]:
        return list(self._versions.get(app, []))

    async def version(self, app: str, version: int) -> AppVersion | None:
        return next((v for v in self._versions.get(app, []) if v.version == version), None)

    async def draft(self, app: str) -> Draft | None:
        return self._drafts.get(app)

    async def save_draft(self, app: str, config: dict[str, Any], actor: str | None) -> Draft:
        self._drafts[app] = Draft(app, config, uuid.uuid4().hex, actor)
        return self._drafts[app]

    async def discard_draft(self, app: str) -> None:
        if self._drafts.pop(app, None) is None:
            raise NoDraft(app)

    async def set_draft_eval(self, app: str, revision: str, eval: dict[str, Any]) -> bool:
        draft = self._drafts.get(app)
        if draft is None or draft.revision != revision:
            return False
        self._drafts[app] = replace(draft, eval=eval)
        return True

    async def publish(
        self, app: str, note: str, actor: str | None, revision: str | None = None
    ) -> AppVersion:
        draft = self._drafts.get(app)
        if draft is None:
            raise NoDraft(app)
        if revision is not None and draft.revision != revision:
            raise DraftChanged(app)
        del self._drafts[app]
        versions = self._versions[app]
        number = versions[0].version + 1
        published = AppVersion(app, number, draft.config, note, actor, eval=draft.eval)
        versions.insert(0, published)
        self._current[app] = published.version
        return published

    async def activate(self, app: str, version: int) -> None:
        if await self.version(app, version) is None:
            raise VersionNotFound(f"{app} v{version}")
        self._current[app] = version


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS app_versions (
    tenant_id  text NOT NULL DEFAULT 'default',
    app        text NOT NULL,
    version    integer NOT NULL,
    config     jsonb NOT NULL,
    note       text,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    eval       jsonb,
    PRIMARY KEY (tenant_id, app, version)
)""",
    """
CREATE TABLE IF NOT EXISTS app_heads (
    tenant_id text NOT NULL DEFAULT 'default',
    app       text NOT NULL,
    current   integer NOT NULL,
    PRIMARY KEY (tenant_id, app)
)""",
    """
CREATE TABLE IF NOT EXISTS app_drafts (
    tenant_id  text NOT NULL DEFAULT 'default',
    app        text NOT NULL,
    config     jsonb NOT NULL,
    revision   text NOT NULL,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    eval       jsonb,
    PRIMARY KEY (tenant_id, app)
)""",
]

VERSION_COLUMNS = "app, version, config, note, created_by, created_at, eval"
DRAFT_COLUMNS = "app, config, revision, created_by, created_at, eval"


class PostgresAppVersionStore:
    """App versions in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def _lock(self, conn: Any, app: str) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"apps:{DEFAULT_TENANT}:{app}",),
        )

    async def ensure(self, app: str, config: dict[str, Any]) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO app_versions (tenant_id, app, version, config, note)"
                " VALUES (%s, %s, 1, %s, %s) ON CONFLICT DO NOTHING",
                (DEFAULT_TENANT, app, Jsonb(config), DEFAULT_NOTE),
            )
            await conn.execute(
                "INSERT INTO app_heads (tenant_id, app, current) VALUES (%s, %s, 1)"
                " ON CONFLICT DO NOTHING",
                (DEFAULT_TENANT, app),
            )

    async def current(self, app: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT current FROM app_heads WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            row = await cur.fetchone()
            if row is None:
                raise VersionNotFound(app)
            return row["current"]

    async def versions(self, app: str) -> list[AppVersion]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {VERSION_COLUMNS} FROM app_versions WHERE tenant_id = %s AND app = %s"
                " ORDER BY version DESC",
                (DEFAULT_TENANT, app),
            )
            return [AppVersion(**row) for row in await cur.fetchall()]

    async def version(self, app: str, version: int) -> AppVersion | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {VERSION_COLUMNS} FROM app_versions"
                " WHERE tenant_id = %s AND app = %s AND version = %s",
                (DEFAULT_TENANT, app, version),
            )
            row = await cur.fetchone()
            return AppVersion(**row) if row else None

    async def draft(self, app: str) -> Draft | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {DRAFT_COLUMNS} FROM app_drafts WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            row = await cur.fetchone()
            return Draft(**row) if row else None

    async def save_draft(self, app: str, config: dict[str, Any], actor: str | None) -> Draft:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO app_drafts (tenant_id, app, config, revision, created_by)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, app) DO UPDATE SET"
                " config = excluded.config, revision = excluded.revision,"
                " created_by = excluded.created_by, created_at = now(), eval = NULL"
                f" RETURNING {DRAFT_COLUMNS}",
                (DEFAULT_TENANT, app, Jsonb(config), uuid.uuid4().hex, actor),
            )
            return Draft(**await cur.fetchone())

    async def discard_draft(self, app: str) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM app_drafts WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            if cur.rowcount == 0:
                raise NoDraft(app)

    async def set_draft_eval(self, app: str, revision: str, eval: dict[str, Any]) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE app_drafts SET eval = %s"
                " WHERE tenant_id = %s AND app = %s AND revision = %s",
                (Jsonb(eval), DEFAULT_TENANT, app, revision),
            )
            return cur.rowcount > 0

    async def publish(
        self, app: str, note: str, actor: str | None, revision: str | None = None
    ) -> AppVersion:
        """Publish the draft; with ``revision``, only if it is still that revision."""
        async with self._pool.connection() as conn, conn.transaction():
            await self._lock(conn, app)
            cur = await conn.execute(
                "DELETE FROM app_drafts WHERE tenant_id = %s AND app = %s"
                " AND (%s::text IS NULL OR revision = %s) RETURNING config, eval",
                (DEFAULT_TENANT, app, revision, revision),
            )
            draft = await cur.fetchone()
            if draft is None:
                if revision is not None and await self.draft(app) is not None:
                    raise DraftChanged(app)
                raise NoDraft(app)
            cur = await conn.execute(
                "INSERT INTO app_versions (tenant_id, app, version, config, note, created_by, eval)"
                " SELECT %s, %s, coalesce(max(version), 0) + 1, %s, %s, %s, %s"
                " FROM app_versions WHERE tenant_id = %s AND app = %s"
                f" RETURNING {VERSION_COLUMNS}",
                (
                    DEFAULT_TENANT,
                    app,
                    Jsonb(draft["config"]),
                    note,
                    actor,
                    Jsonb(draft["eval"]) if draft["eval"] is not None else None,
                    DEFAULT_TENANT,
                    app,
                ),
            )
            published = AppVersion(**await cur.fetchone())
            await conn.execute(
                "UPDATE app_heads SET current = %s WHERE tenant_id = %s AND app = %s",
                (published.version, DEFAULT_TENANT, app),
            )
            return published

    async def activate(self, app: str, version: int) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await self._lock(conn, app)
            cur = await conn.execute(
                "SELECT 1 FROM app_versions WHERE tenant_id = %s AND app = %s AND version = %s",
                (DEFAULT_TENANT, app, version),
            )
            if await cur.fetchone() is None:
                raise VersionNotFound(f"{app} v{version}")
            await conn.execute(
                "UPDATE app_heads SET current = %s WHERE tenant_id = %s AND app = %s",
                (version, DEFAULT_TENANT, app),
            )
