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
    # Who asked for the run, as they said when starting it (until sign-in exists).
    created_by: str | None = None
    # The node the run is at, or the last one it reached: ``node`` and ``title``.
    step: dict[str, str] | None = None
    # When the current wait for a person began (while waiting), and the seconds spent
    # in waits already over. Runs from before this was counted have None.
    waiting_since: datetime | None = None
    wait_seconds: float | None = 0.0


class RunStore(Protocol):
    async def setup(self) -> None: ...
    async def create(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None = None,
        config: dict[str, Any] | None = None,
        created_by: str | None = None,
    ) -> RunRecord: ...
    async def get(self, run_id: str) -> RunRecord | None: ...
    async def recent(self, limit: int = 50) -> list[RunRecord]: ...
    async def list_by_status(self, status: RunStatus) -> list[RunRecord]: ...
    async def set_status(self, run_id: str, status: RunStatus, error: str | None = None) -> None:
        """Also keeps the waiting time: entering ``waiting`` starts a wait, leaving it
        adds the wait to ``wait_seconds``."""
        ...

    async def set_step(self, run_id: str, node: str, title: str) -> None: ...


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
        created_by: str | None = None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=str(uuid.uuid4()),
            app=app,
            status="running",
            input=input,
            app_version=app_version,
            config=config,
            created_by=created_by,
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
        run, now = self._runs[run_id], datetime.now(UTC)
        waited, since = run.wait_seconds, None
        if status == "waiting":
            since = run.waiting_since if run.status == "waiting" and run.waiting_since else now
        elif run.status == "waiting" and run.waiting_since and waited is not None:
            waited += (now - run.waiting_since).total_seconds()
        self._runs[run_id] = replace(
            run,
            status=status,
            error=error,
            updated_at=now,
            waiting_since=since,
            wait_seconds=waited,
        )

    async def set_step(self, run_id: str, node: str, title: str) -> None:
        self._runs[run_id] = replace(self._runs[run_id], step={"node": node, "title": title})


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
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS created_by text",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS step jsonb",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS waiting_since timestamptz",
    # No default: runs from before waits were counted keep NULL (unknown).
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS wait_seconds double precision",
]

COLUMNS = (
    "run_id::text, app, status, input, tenant_id, error, app_version, config, created_at,"
    " updated_at, created_by, step, waiting_since, wait_seconds"
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
        created_by: str | None = None,
    ) -> RunRecord:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO runs"
                " (run_id, app, status, input, app_version, config, created_by, wait_seconds)"
                f" VALUES (%s, %s, 'running', %s, %s, %s, %s, 0) RETURNING {COLUMNS}",
                (
                    uuid.uuid4(),
                    app,
                    Jsonb(input),
                    app_version,
                    Jsonb(config) if config is not None else None,
                    created_by,
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
        # The right-hand sides read the row as it was before this update.
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = %(status)s, error = %(error)s, updated_at = now(),"
                " wait_seconds = wait_seconds + CASE"
                "   WHEN status = 'waiting' AND %(status)s <> 'waiting'"
                "   THEN coalesce(extract(epoch FROM now() - waiting_since), 0) ELSE 0 END,"
                " waiting_since = CASE"
                "   WHEN %(status)s <> 'waiting' THEN NULL"
                "   WHEN status = 'waiting' THEN coalesce(waiting_since, now())"
                "   ELSE now() END"
                " WHERE run_id = %(run_id)s",
                {"status": status, "error": error, "run_id": uuid.UUID(run_id)},
            )

    async def set_step(self, run_id: str, node: str, title: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET step = %s WHERE run_id = %s",
                (Jsonb({"node": node, "title": title}), uuid.UUID(run_id)),
            )
