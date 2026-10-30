"""Registered data sources and tool services. Each one points at a connector MCP server.

Credentials never live here: the connector holds them (local .env, Key Vault on
Azure). The platform only knows where the connector is and what it is called. Data
sources (databases and uploaded files) are what ETL reads, through ``ETL_TOOLS``; a
tool service (kind ``mcp``) only offers tools to flow apps' tool and agent nodes
(docs/api/datasources.md).
"""

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg import errors
from psycopg_pool import AsyncConnectionPool

from aep.connector_urls import same_url
from aep.runtime.store import DEFAULT_TENANT

DataSourceKind = Literal["postgres", "mysql", "sqlserver", "file", "mcp"]
# The kinds ETL reads from; each must offer ETL_TOOLS. The other kind, mcp, is a tool service.
DATABASE_KINDS: tuple[str, ...] = ("postgres", "mysql", "sqlserver", "file")
# What ETL reads through a data source's connector (connectors/postgres and the others).
ETL_TOOLS = (
    "list_schemas",
    "list_tables",
    "describe_table",
    "table_stats",
    "sample_rows",
    "read_rows",
)


def canon_id(value: str) -> str:
    """An id as stored: a UUID in its usual lowercase form (written in any case, or in
    braces), anything else trimmed."""
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return value.strip()


class DuplicateName(Exception):
    pass


@dataclass(frozen=True)
class DataSource:
    datasource_id: str
    name: str
    kind: DataSourceKind
    connector_url: str
    description: str = ""
    tenant_id: str = DEFAULT_TENANT
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class DataSourceStore(Protocol):
    async def setup(self) -> None: ...
    async def create(
        self, name: str, kind: DataSourceKind, connector_url: str, description: str = ""
    ) -> DataSource: ...
    async def get(self, datasource_id: str) -> DataSource | None: ...
    async def all(self) -> list[DataSource]: ...
    async def update(
        self,
        datasource_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        connector_url: str | None = None,
    ) -> DataSource | None: ...


class MemoryDataSourceStore:
    """In-process store for tests."""

    def __init__(self) -> None:
        self._items: dict[str, DataSource] = {}

    async def setup(self) -> None:
        return None

    async def create(
        self, name: str, kind: DataSourceKind, connector_url: str, description: str = ""
    ) -> DataSource:
        if any(d.name == name for d in self._items.values()):
            raise DuplicateName(name)
        ds = DataSource(str(uuid.uuid4()), name, kind, connector_url, description)
        self._items[ds.datasource_id] = ds
        return ds

    async def get(self, datasource_id: str) -> DataSource | None:
        return self._items.get(datasource_id)

    async def all(self) -> list[DataSource]:
        return sorted(self._items.values(), key=lambda d: d.name)

    async def update(
        self,
        datasource_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        connector_url: str | None = None,
    ) -> DataSource | None:
        ds = self._items.get(datasource_id)
        if ds is None:
            return None
        others = (d.name for d in self._items.values() if d.datasource_id != datasource_id)
        if name is not None and name in others:
            raise DuplicateName(name)
        changes = {"name": name, "description": description, "connector_url": connector_url}
        ds = dataclasses.replace(ds, **{k: v for k, v in changes.items() if v is not None})
        self._items[datasource_id] = ds
        return ds


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS datasources (
    datasource_id uuid PRIMARY KEY,
    tenant_id     text NOT NULL DEFAULT 'default',
    name          text NOT NULL,
    kind          text NOT NULL,
    connector_url text NOT NULL,
    description   text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
)""",
]

COLUMNS = "datasource_id::text, name, kind, connector_url, description, tenant_id, created_at"


def _record(row: dict[str, Any]) -> DataSource:
    return DataSource(**row)


async def register_defaults(store: DataSourceStore, defaults: list[dict[str, str]]) -> int:
    """Register each default whose name is not taken yet and whose connector no source
    uses yet (a default renamed on the page keeps its address); returns how many were
    added."""
    registered = await store.all()
    taken = {d.name for d in registered}
    added = 0
    for item in defaults:
        if item["name"] in taken or any(
            same_url(item["connector_url"], d.connector_url) for d in registered
        ):
            continue
        try:
            await store.create(
                item["name"], item["kind"], item["connector_url"], item.get("description", "")
            )
        except DuplicateName:  # another process registered it first
            continue
        added += 1
    return added


class PostgresDataSourceStore:
    """Data sources in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def create(
        self, name: str, kind: DataSourceKind, connector_url: str, description: str = ""
    ) -> DataSource:
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "INSERT INTO datasources"
                    " (datasource_id, name, kind, connector_url, description)"
                    f" VALUES (%s, %s, %s, %s, %s) RETURNING {COLUMNS}",
                    (uuid.uuid4(), name, kind, connector_url, description),
                )
            except errors.UniqueViolation as exc:
                raise DuplicateName(name) from exc
            return _record(await cur.fetchone())

    async def get(self, datasource_id: str) -> DataSource | None:
        try:
            key = uuid.UUID(datasource_id)
        except ValueError:
            return None
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM datasources WHERE datasource_id = %s", (key,)
            )
            row = await cur.fetchone()
            return _record(row) if row else None

    async def all(self) -> list[DataSource]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {COLUMNS} FROM datasources ORDER BY name")
            return [_record(row) for row in await cur.fetchall()]

    async def update(
        self,
        datasource_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        connector_url: str | None = None,
    ) -> DataSource | None:
        try:
            key = uuid.UUID(datasource_id)
        except ValueError:
            return None
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "UPDATE datasources SET name = coalesce(%s, name),"
                    " description = coalesce(%s, description),"
                    " connector_url = coalesce(%s, connector_url)"
                    f" WHERE datasource_id = %s AND tenant_id = %s RETURNING {COLUMNS}",
                    (name, description, connector_url, key, DEFAULT_TENANT),
                )
            except errors.UniqueViolation as exc:
                raise DuplicateName(name) from exc
            row = await cur.fetchone()
            return _record(row) if row else None
