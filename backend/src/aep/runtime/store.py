"""Run records: which app a run belongs to, its input and its lifecycle status.

The graph state itself (messages, pending approvals, results) lives in the
LangGraph checkpointer; this table only tracks what the API needs to list and
route runs.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

RunStatus = Literal["running", "waiting", "done", "error", "cancelled"]
# A run in one of these has ended; only a new run (a rerun) goes on from there.
FINISHED: tuple[RunStatus, ...] = ("done", "error", "cancelled")

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
    # What kind of failure stopped it (aep.runtime.flow.nodes.ERROR_TYPES, or
    # ``max_visits``/``max_steps``; None when unknown), who cancelled it, and the run
    # it was copied from.
    error_type: str | None = None
    cancelled_by: str | None = None
    rerun_of: str | None = None
    # What a ``done`` flow run came to: the ``outcome`` of the end node it ended at
    # (``delivered``, ``stopped`` or another word the app chose); None otherwise.
    outcome: str | None = None
    # The data outbound policy in force for the run (aep.policy), fixed when it starts;
    # None for runs from before policies existed (they sent everything).
    policy: dict[str, Any] | None = None
    # The snapshot of the project the run binds (aep.projects.snapshot), or None.
    project: dict[str, Any] | None = None
    # Where a run rerun from a step of another run began: ``node``, ``title`` and the
    # source ``run_id`` (docs/api/evals.md, "从某一步重跑"); None for other runs.
    start_at: dict[str, Any] | None = None
    # What started the run when no person did: ``{"kind": "schedule", "schedule_id",
    # "title"}`` (docs/api/schedules.md); None for other runs.
    trigger: dict[str, Any] | None = None
    # The conversation the run is a turn of, and which turn (from 1); None outside one
    # (docs/api/conversations.md).
    conversation_id: str | None = None
    conversation_turn: int | None = None
    # When its content (input, state, error) was cleared with its conversation.
    cleared_at: datetime | None = None


class TurnTaken(Exception):
    """Another run already is that turn of the conversation."""


class RunStore(Protocol):
    async def setup(self) -> None: ...
    async def create(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None = None,
        config: dict[str, Any] | None = None,
        created_by: str | None = None,
        rerun_of: str | None = None,
        policy: dict[str, Any] | None = None,
        project: dict[str, Any] | None = None,
        start_at: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        conversation_turn: int | None = None,
    ) -> RunRecord:
        """Raises ``TurnTaken`` when that turn of the conversation exists already."""
        ...

    async def get(self, run_id: str) -> RunRecord | None: ...
    async def recent(self, limit: int = 50) -> list[RunRecord]: ...
    async def list_by_status(self, status: RunStatus) -> list[RunRecord]: ...
    async def set_status(
        self,
        run_id: str,
        status: RunStatus,
        error: str | None = None,
        *,
        expected: tuple[RunStatus, ...] | None = None,
        error_type: str | None = None,
        actor: str | None = None,
        outcome: str | None = None,
    ) -> bool:
        """Change the status, only from one of ``expected`` if given; False when the run
        was in another status (someone else changed it first) and nothing changed.

        Also keeps the waiting time: entering ``waiting`` starts a wait, leaving it
        adds the wait to ``wait_seconds``; so does leaving ``error`` (a retry): the
        failure waited for someone to retry it. ``actor`` is who cancelled, for ``cancelled``;
        ``outcome`` what a ``done`` run came to (every change sets it, None by default)."""
        ...

    async def set_step(self, run_id: str, node: str, title: str) -> None: ...
    async def called_by(self, run_id: str, node: str) -> RunRecord | None:
        """The newest unfinished run that node ``node`` of run ``run_id`` started (an app
        node; its ``trigger`` says so), or None."""
        ...

    async def outcomes(self, since: datetime) -> dict[str, tuple[int, int]]:
        """Per app, how many runs created since ``since`` ended ``done`` and ``error``
        (cancelled and unfinished runs do not count): ``{app: (done, error)}``."""
        ...

    async def stats(self, since: datetime) -> list["RunStat"]:
        """Runs created since ``since``, without a draft's trial and evaluation runs, for
        the operations dashboard (docs/api/ops.md)."""
        ...

    async def apps_of(self, run_ids: list[str]) -> dict[str, str]:
        """The app of each of these runs that exists."""
        ...

    async def in_conversation(self, conversation_id: str) -> list[RunRecord]:
        """The conversation's turns, the first first."""
        ...

    async def clear(self, run_ids: list[str]) -> None:
        """Clear what these runs were about (input, error) and mark them cleared; their
        status, version, times and usage stay."""
        ...


@dataclass(frozen=True)
class RunStat:
    run_id: str
    app: str
    status: RunStatus
    outcome: str | None
    rerun_of: str | None
    created_at: datetime
    updated_at: datetime
    wait_seconds: float | None
    reran: bool  # another run was copied from this one


def _counted(record: RunRecord) -> bool:
    # A draft's trial and evaluation runs belong to no version (as for notifications).
    return not (record.app_version is None and record.config is not None)


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
        rerun_of: str | None = None,
        policy: dict[str, Any] | None = None,
        project: dict[str, Any] | None = None,
        start_at: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        conversation_turn: int | None = None,
    ) -> RunRecord:
        if conversation_id is not None and any(
            r.conversation_id == conversation_id and r.conversation_turn == conversation_turn
            for r in self._runs.values()
        ):
            raise TurnTaken(conversation_id)
        record = RunRecord(
            run_id=str(uuid.uuid4()),
            app=app,
            status="running",
            input=input,
            app_version=app_version,
            config=config,
            created_by=created_by,
            rerun_of=rerun_of,
            policy=policy,
            project=project,
            start_at=start_at,
            trigger=trigger,
            conversation_id=conversation_id,
            conversation_turn=conversation_turn,
        )
        self._runs[record.run_id] = record
        return record

    async def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    async def called_by(self, run_id: str, node: str) -> RunRecord | None:
        found = [
            r
            for r in self._runs.values()
            if r.status in ("running", "waiting")
            and (r.trigger or {}).get("kind") == "app"
            and r.trigger.get("run_id") == run_id  # type: ignore[union-attr]
            and r.trigger.get("node") == node  # type: ignore[union-attr]
        ]
        return max(found, key=lambda r: r.created_at) if found else None

    async def outcomes(self, since: datetime) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        for r in self._runs.values():
            if r.created_at >= since and r.status in ("done", "error"):
                done, error = found.get(r.app, (0, 0))
                found[r.app] = (done + 1, error) if r.status == "done" else (done, error + 1)
        return found

    async def stats(self, since: datetime) -> list[RunStat]:
        copied = {r.rerun_of for r in self._runs.values() if r.rerun_of}
        return [
            RunStat(
                r.run_id,
                r.app,
                r.status,
                r.outcome,
                r.rerun_of,
                r.created_at,
                r.updated_at,
                r.wait_seconds,
                r.run_id in copied,
            )
            for r in self._runs.values()
            if r.created_at >= since and _counted(r)
        ]

    async def apps_of(self, run_ids: list[str]) -> dict[str, str]:
        return {key: self._runs[key].app for key in run_ids if key in self._runs}

    async def in_conversation(self, conversation_id: str) -> list[RunRecord]:
        found = [r for r in self._runs.values() if r.conversation_id == conversation_id]
        return sorted(found, key=lambda r: r.conversation_turn or 0)

    async def clear(self, run_ids: list[str]) -> None:
        now = datetime.now(UTC)
        for key in run_ids:
            if key in self._runs:
                self._runs[key] = replace(self._runs[key], input={}, error=None, cleared_at=now)

    async def recent(self, limit: int = 50) -> list[RunRecord]:
        runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    async def list_by_status(self, status: RunStatus) -> list[RunRecord]:
        return [r for r in self._runs.values() if r.status == status]

    async def set_status(
        self,
        run_id: str,
        status: RunStatus,
        error: str | None = None,
        *,
        expected: tuple[RunStatus, ...] | None = None,
        error_type: str | None = None,
        actor: str | None = None,
        outcome: str | None = None,
    ) -> bool:
        run, now = self._runs[run_id], datetime.now(UTC)
        if expected is not None and run.status not in expected:
            return False
        waited, since = run.wait_seconds, None
        if status == "waiting":
            since = run.waiting_since if run.status == "waiting" and run.waiting_since else now
        elif run.status == "waiting" and run.waiting_since and waited is not None:
            waited += (now - run.waiting_since).total_seconds()
        elif run.status == "error" and status != "error" and waited is not None:
            # A failure waits for someone to retry it.
            waited += (now - run.updated_at).total_seconds()
        self._runs[run_id] = replace(
            run,
            status=status,
            error=error,
            updated_at=now,
            waiting_since=since,
            wait_seconds=waited,
            error_type=error_type,
            cancelled_by=actor if status == "cancelled" else run.cancelled_by,
            outcome=outcome,
        )
        return True

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
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS error_type text",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS cancelled_by text",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS rerun_of uuid",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS outcome text",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS policy jsonb",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS project jsonb",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS start_at jsonb",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS trigger jsonb",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS conversation_id text",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS conversation_turn integer",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS cleared_at timestamptz",
    # One run per turn: two messages sent at once cannot both be the next turn.
    "CREATE UNIQUE INDEX IF NOT EXISTS runs_conversation_turn_idx"
    " ON runs (conversation_id, conversation_turn) WHERE conversation_id IS NOT NULL",
]

COLUMNS = (
    "run_id::text, app, status, input, tenant_id, error, app_version, config, created_at,"
    " updated_at, created_by, step, waiting_since, wait_seconds, error_type, cancelled_by,"
    " rerun_of::text, outcome, policy, project, start_at, trigger, conversation_id,"
    " conversation_turn, cleared_at"
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
        rerun_of: str | None = None,
        policy: dict[str, Any] | None = None,
        project: dict[str, Any] | None = None,
        start_at: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        conversation_turn: int | None = None,
    ) -> RunRecord:
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "INSERT INTO runs (run_id, app, status, input, app_version, config,"
                    " created_by, wait_seconds, rerun_of, policy, project, start_at, trigger,"
                    " conversation_id, conversation_turn)"
                    " VALUES (%s, %s, 'running', %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s)"
                    f" RETURNING {COLUMNS}",
                    (
                        uuid.uuid4(),
                        app,
                        Jsonb(input),
                        app_version,
                        Jsonb(config) if config is not None else None,
                        created_by,
                        uuid.UUID(rerun_of) if rerun_of else None,
                        Jsonb(policy) if policy is not None else None,
                        Jsonb(project) if project is not None else None,
                        Jsonb(start_at) if start_at is not None else None,
                        Jsonb(trigger) if trigger is not None else None,
                        conversation_id,
                        conversation_turn,
                    ),
                )
            except UniqueViolation as exc:
                raise TurnTaken(str(conversation_id)) from exc
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

    async def called_by(self, run_id: str, node: str) -> RunRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM runs WHERE status IN ('running', 'waiting')"
                " AND trigger->>'kind' = 'app' AND trigger->>'run_id' = %s"
                " AND trigger->>'node' = %s ORDER BY created_at DESC LIMIT 1",
                (run_id, node),
            )
            row = await cur.fetchone()
            return _record(row) if row else None

    async def outcomes(self, since: datetime) -> dict[str, tuple[int, int]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT app, count(*) FILTER (WHERE status = 'done') AS done,"
                " count(*) FILTER (WHERE status = 'error') AS error FROM runs"
                " WHERE created_at >= %s AND status IN ('done', 'error') GROUP BY app",
                (since,),
            )
            return {row["app"]: (row["done"], row["error"]) for row in await cur.fetchall()}

    async def stats(self, since: datetime) -> list[RunStat]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT r.run_id::text, r.app, r.status, r.outcome, r.rerun_of::text,"
                " r.created_at, r.updated_at, r.wait_seconds,"
                " EXISTS (SELECT 1 FROM runs c WHERE c.rerun_of = r.run_id) AS reran"
                " FROM runs r WHERE r.created_at >= %s"
                " AND NOT (r.app_version IS NULL AND r.config IS NOT NULL)",
                (since,),
            )
            return [RunStat(**row) for row in await cur.fetchall()]

    async def apps_of(self, run_ids: list[str]) -> dict[str, str]:
        keys = [key for key in run_ids if _uuid(key)]
        if not keys:
            return {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id::text, app FROM runs WHERE run_id = ANY(%s::uuid[])", (keys,)
            )
            return {row["run_id"]: row["app"] for row in await cur.fetchall()}

    async def in_conversation(self, conversation_id: str) -> list[RunRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM runs WHERE conversation_id = %s ORDER BY conversation_turn",
                (conversation_id,),
            )
            return [_record(row) for row in await cur.fetchall()]

    async def clear(self, run_ids: list[str]) -> None:
        keys = [key for key in run_ids if _uuid(key)]
        if not keys:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET input = '{}', error = NULL, cleared_at = now()"
                " WHERE run_id = ANY(%s::uuid[])",
                (keys,),
            )

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

    async def set_status(
        self,
        run_id: str,
        status: RunStatus,
        error: str | None = None,
        *,
        expected: tuple[RunStatus, ...] | None = None,
        error_type: str | None = None,
        actor: str | None = None,
        outcome: str | None = None,
    ) -> bool:
        # The right-hand sides and the WHERE clause read the row as it was before this
        # update, and the row is locked while it changes: of two updates expecting the
        # same status, only the first one finds it.
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE runs SET status = %(status)s, error = %(error)s, updated_at = now(),"
                " error_type = %(error_type)s, outcome = %(outcome)s,"
                " cancelled_by = CASE WHEN %(status)s = 'cancelled' THEN %(actor)s"
                "   ELSE cancelled_by END,"
                " wait_seconds = wait_seconds + CASE"
                "   WHEN status = 'waiting' AND %(status)s <> 'waiting'"
                "   THEN coalesce(extract(epoch FROM now() - waiting_since), 0)"
                "   WHEN status = 'error' AND %(status)s <> 'error'"
                "   THEN extract(epoch FROM now() - updated_at) ELSE 0 END,"
                " waiting_since = CASE"
                "   WHEN %(status)s <> 'waiting' THEN NULL"
                "   WHEN status = 'waiting' THEN coalesce(waiting_since, now())"
                "   ELSE now() END"
                " WHERE run_id = %(run_id)s"
                " AND (%(any)s OR status = ANY(%(expected)s))",
                {
                    "status": status,
                    "error": error,
                    "error_type": error_type,
                    "actor": actor,
                    "outcome": outcome,
                    "run_id": uuid.UUID(run_id),
                    "any": expected is None,
                    "expected": list(expected or ()),
                },
            )
            return cur.rowcount > 0

    async def set_step(self, run_id: str, node: str, title: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET step = %s WHERE run_id = %s",
                (Jsonb({"node": node, "title": title}), uuid.UUID(run_id)),
            )


def _uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
