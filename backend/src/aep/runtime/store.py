"""Run records: which app a run belongs to, its input and its lifecycle status.

The graph state itself (messages, pending approvals, results) lives in the
LangGraph checkpointer; this table only tracks what the API needs to list and
route runs.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

RunStatus = Literal["running", "waiting", "done", "error"]

DEFAULT_TENANT = "default"


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    app: str
    status: RunStatus
    input: dict[str, Any]
    tenant_id: str = DEFAULT_TENANT
    error: str | None = None
    # The app version the run uses (None: a draft, being evaluated) and that
    # version's configuration, kept with the run so a restart picks up the same one.
    # Runs from before app versions have neither and use the app's defaults.
    app_version: int | None = None
    config: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class RunStore(Protocol):
    async def setup(self) -> None: ...
    async def create(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> RunRecord: ...
    async def get(self, run_id: str) -> RunRecord | None: ...
    async def recent(self, limit: int = 50) -> list[RunRecord]: ...
    async def list_by_status(self, status: RunStatus) -> list[RunRecord]: ...
    async def set_status(
        self, run_id: str, status: RunStatus, error: str | None = None
    ) -> None: ...


class MemoryRunStore:
    """In-process store for tests and quick experiments."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}

    async def setup(self) -> None:
        return None

    async def create(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=str(uuid.uuid4()),
            app=app,
            status="running",
            input=input,
            app_version=app_version,
            config=config,
        )
        self._runs[record.run_id] = record
        return record

    async def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    async def recent(self, limit: int = 50) -> list[RunRecord]:
        runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    async def list_by_status(self, status: RunStatus) -> list[RunRecord]:
        return [r for r in self._runs.values() if r.status == status]

    async def set_status(self, run_id: str, status: RunStatus, error: str | None = None) -> None:
        self._runs[run_id] = replace(
            self._runs[run_id], status=status, error=error, updated_at=datetime.now(UTC)
        )


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS runs (
    run_id     uuid PRIMARY KEY,
    tenant_id  text NOT NULL DEFAULT 'default',
    app        text NOT NULL,
    status     text NOT NULL,
    input      jsonb NOT NULL,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)""",
    "CREATE INDEX IF NOT EXISTS runs_tenant_created_idx ON runs (tenant_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status)",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS app_version integer",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS config jsonb",
]

COLUMNS = (
    "run_id::text, app, status, input, tenant_id, error, app_version, config, created_at,"
    " updated_at"
)


def _record(row: dict[str, Any]) -> RunRecord:
    return RunRecord(**row)


class PostgresRunStore:
    """Run records in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def create(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> RunRecord:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO runs (run_id, app, status, input, app_version, config)"
                f" VALUES (%s, %s, 'running', %s, %s, %s) RETURNING {COLUMNS}",
                (
                    uuid.uuid4(),
                    app,
                    Jsonb(input),
                    app_version,
                    Jsonb(config) if config is not None else None,
                ),
            )
            return _record(await cur.fetchone())

    async def get(self, run_id: str) -> RunRecord | None:
        try:
            key = uuid.UUID(run_id)
        except ValueError:
            return None
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {COLUMNS} FROM runs WHERE run_id = %s", (key,))
            row = await cur.fetchone()
            return _record(row) if row else None

    async def recent(self, limit: int = 50) -> list[RunRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM runs ORDER BY created_at DESC LIMIT %s", (limit,)
            )
            return [_record(row) for row in await cur.fetchall()]

    async def list_by_status(self, status: RunStatus) -> list[RunRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {COLUMNS} FROM runs WHERE status = %s", (status,))
            return [_record(row) for row in await cur.fetchall()]

    async def set_status(self, run_id: str, status: RunStatus, error: str | None = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = %s, error = %s, updated_at = now() WHERE run_id = %s",
                (status, error, uuid.UUID(run_id)),
            )
