"""People's "right / wrong" on finished runs (docs/api/evals.md, "运行反馈").

One rating per person per run; with sign-in off everyone is the same person (``""``).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import DEFAULT_TENANT

Rating = Literal["good", "bad"]


@dataclass
class Feedback:
    run_id: str
    who: str  # the user's id, or "" with sign-in off
    rating: Rating
    note: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FeedbackStore(Protocol):
    async def setup(self) -> None: ...
    async def put(self, feedback: Feedback) -> None: ...
    async def delete(self, run_id: str, who: str) -> None: ...
    async def for_run(self, run_id: str) -> list[Feedback]: ...
    async def for_runs(self, run_ids: list[str]) -> list[Feedback]: ...


class MemoryFeedbackStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], Feedback] = {}

    async def setup(self) -> None:
        pass

    async def put(self, feedback: Feedback) -> None:
        self.items[(feedback.run_id, feedback.who)] = feedback

    async def delete(self, run_id: str, who: str) -> None:
        self.items.pop((run_id, who), None)

    async def for_run(self, run_id: str) -> list[Feedback]:
        return [f for (run, _), f in self.items.items() if run == run_id]

    async def for_runs(self, run_ids: list[str]) -> list[Feedback]:
        wanted = set(run_ids)
        return [f for (run, _), f in self.items.items() if run in wanted]


SCHEMA = """
CREATE TABLE IF NOT EXISTS run_feedback (
    tenant_id  text NOT NULL DEFAULT 'default',
    run_id     text NOT NULL,
    who        text NOT NULL,
    rating     text NOT NULL,
    note       text,
    at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, who)
)"""


class PostgresFeedbackStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def put(self, feedback: Feedback) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO run_feedback (tenant_id, run_id, who, rating, note, at)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, run_id, who)"
                " DO UPDATE SET rating = EXCLUDED.rating, note = EXCLUDED.note, at = EXCLUDED.at",
                (
                    DEFAULT_TENANT,
                    feedback.run_id,
                    feedback.who,
                    feedback.rating,
                    feedback.note,
                    feedback.at,
                ),
            )

    async def delete(self, run_id: str, who: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM run_feedback WHERE tenant_id = %s AND run_id = %s AND who = %s",
                (DEFAULT_TENANT, run_id, who),
            )

    async def for_run(self, run_id: str) -> list[Feedback]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, who, rating, note, at FROM run_feedback"
                " WHERE tenant_id = %s AND run_id = %s",
                (DEFAULT_TENANT, run_id),
            )
            return [Feedback(**row) for row in await cur.fetchall()]

    async def for_runs(self, run_ids: list[str]) -> list[Feedback]:
        if not run_ids:
            return []
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, who, rating, note, at FROM run_feedback"
                " WHERE tenant_id = %s AND run_id = ANY(%s)",
                (DEFAULT_TENANT, run_ids),
            )
            return [Feedback(**row) for row in await cur.fetchall()]


def summary(items: list[Feedback], who: str) -> dict[str, Any]:
    mine = next((f for f in items if f.who == who), None)
    return {
        "good": sum(f.rating == "good" for f in items),
        "bad": sum(f.rating == "bad" for f in items),
        "mine": {"rating": mine.rating, "note": mine.note, "at": mine.at.isoformat()}
        if mine
        else None,
    }
