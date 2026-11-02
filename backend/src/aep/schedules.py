"""Runs started on a schedule (docs/api/schedules.md).

Each schedule belongs to an app and says when (a five-field cron in a time zone) and
with what (the run's input and project). The ``Scheduler`` checks for schedules that
are due every ``AEP_SCHEDULE_POLL_SECONDS``, moves each to its next time first and then
starts the run; several API processes may check at once, each due schedule is claimed
by one of them (``FOR UPDATE SKIP LOCKED``). The cron expressions are parsed here, with
no dependency.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import DEFAULT_TENANT, RunRecord

log = logging.getLogger(__name__)

MAX_PER_APP = 20
# How far ahead a schedule's next time is looked for (a 29 February needs four years).
SEARCH_DAYS = 366 * 8

FIELDS = (("分", 0, 59), ("时", 0, 23), ("日", 1, 31), ("月", 1, 12), ("周", 0, 7))


class CronError(ValueError):
    """A cron expression that cannot be read; the message says why, for people."""


@dataclass(frozen=True)
class Cron:
    """A five-field cron expression: minute, hour, day of month, month, day of week
    (0 and 7 are Sunday). When both days are restricted either one will do."""

    text: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, text: str) -> "Cron":
        parts = text.split()
        if len(parts) != 5:
            raise CronError("定时写法不对：要写 5 段（分 时 日 月 周），用空格分开")
        values = [_field(part, n) for n, part in enumerate(parts)]
        weekdays = frozenset(0 if d == 7 else d for d in values[4])
        return cls(
            " ".join(parts),
            tuple(sorted(values[0])),
            tuple(sorted(values[1])),
            frozenset(values[2]),
            frozenset(values[3]),
            weekdays,
            parts[2] == "*",
            parts[4] == "*",
        )

    def _day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        on_day = day.day in self.days
        # Python counts Monday as 0; cron counts Sunday as 0.
        on_weekday = (day.weekday() + 1) % 7 in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return on_weekday
        if self.any_weekday:
            return on_day
        return on_day or on_weekday

    def next_after(self, after: datetime, zone: ZoneInfo) -> datetime | None:
        """The first time after ``after`` (aware) that the expression names, in UTC; a
        local time a clock change skips is passed over, one it repeats is taken once."""
        start = after.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
        start += timedelta(minutes=1)
        day = start.date()
        for _ in range(SEARCH_DAYS):
            if self._day(day):
                for hour in self.hours:
                    if day == start.date() and hour < start.hour:
                        continue
                    for minute in self.minutes:
                        local = datetime.combine(day, time(hour, minute))
                        if local < start:
                            continue
                        aware = local.replace(tzinfo=zone)
                        found = aware.astimezone(UTC)
                        if found.astimezone(zone).replace(tzinfo=None) != local:
                            continue  # skipped by a clock change
                        if found <= after:
                            continue  # the second pass of a repeated hour
                        return found
            day += timedelta(days=1)
        return None

    def upcoming(self, after: datetime, zone: ZoneInfo, count: int = 3) -> list[datetime]:
        found: list[datetime] = []
        at: datetime | None = after
        while at is not None and len(found) < count:
            at = self.next_after(at, zone)
            if at is not None:
                found.append(at)
        return found


def _field(text: str, n: int) -> set[int]:
    name, low, high = FIELDS[n]
    where = f"第 {n + 1} 段（{name}）"
    values: set[int] = set()
    for item in text.split(","):
        body, _, step_text = item.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"定时写法不对：{where}的步长「{step_text}」要是正整数")
            step = int(step_text)
        if body == "*":
            first, last = low, high
        elif "-" in body:
            a, _, b = body.partition("-")
            first, last = _number(a, where, low, high), _number(b, where, low, high)
            if first > last:
                raise CronError(f"定时写法不对：{where}的范围 {body} 从大到小")
        else:
            first = _number(body, where, low, high)
            last = high if step_text else first
        values.update(range(first, last + 1, step))
    return values


def _number(text: str, where: str, low: int, high: int) -> int:
    if not text.isdigit():
        raise CronError(f"定时写法不对：{where}的「{text}」看不懂，只能写数字、*、-、/ 和逗号")
    value = int(text)
    if not low <= value <= high:
        raise CronError(f"定时写法不对：{where}的 {value} 超出 {low}–{high}")
    return value


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"时区 {name} 不认识") from exc


# --- the schedules themselves -------------------------------------------------------


@dataclass
class Schedule:
    schedule_id: str
    app: str
    title: str
    cron: str
    timezone: str
    input: dict[str, Any]
    project: dict[str, Any] | None
    enabled: bool
    created_by: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    next_run_at: datetime | None = None
    last_run_id: str | None = None
    last_run_at: datetime | None = None
    last_error: str | None = None

    def due_after(self, now: datetime) -> datetime | None:
        """When it next runs after ``now``: None when it is off."""
        if not self.enabled:
            return None
        return Cron.parse(self.cron).next_after(now, zone(self.timezone))


def new_id() -> str:
    return f"sch_{uuid.uuid4().hex[:8]}"


class ScheduleStore(Protocol):
    async def setup(self) -> None: ...
    async def for_app(self, app: str) -> list[Schedule]: ...
    async def get(self, schedule_id: str) -> Schedule | None: ...
    async def put(self, schedule: Schedule) -> None: ...
    async def delete(self, schedule_id: str) -> bool: ...
    async def delete_app(self, app: str) -> None: ...
    async def claim_due(self, now: datetime, limit: int = 20) -> list[Schedule]: ...
    async def started(
        self, schedule_id: str, run_id: str | None, at: datetime, error: str | None
    ) -> None: ...


class MemoryScheduleStore:
    def __init__(self) -> None:
        self.items: dict[str, Schedule] = {}

    async def setup(self) -> None:
        pass

    async def for_app(self, app: str) -> list[Schedule]:
        found = [s for s in self.items.values() if s.app == app]
        return sorted(found, key=lambda s: s.created_at)

    async def get(self, schedule_id: str) -> Schedule | None:
        found = self.items.get(schedule_id)
        return replace(found) if found else None

    async def put(self, schedule: Schedule) -> None:
        """Saves a new schedule or a change to one; what the scheduler recorded about its
        runs (``last_*``) stays as it was."""
        found = self.items.get(schedule.schedule_id)
        saved = replace(schedule)
        if found is not None:
            saved.last_run_id, saved.last_run_at = found.last_run_id, found.last_run_at
            saved.last_error = found.last_error
        self.items[schedule.schedule_id] = saved

    async def delete(self, schedule_id: str) -> bool:
        return self.items.pop(schedule_id, None) is not None

    async def delete_app(self, app: str) -> None:
        for key in [k for k, s in self.items.items() if s.app == app]:
            del self.items[key]

    async def claim_due(self, now: datetime, limit: int = 20) -> list[Schedule]:
        due = [
            s
            for s in sorted(self.items.values(), key=lambda s: s.next_run_at or now)
            if s.enabled and s.next_run_at is not None and s.next_run_at <= now
        ][:limit]
        for s in due:
            s.next_run_at = s.due_after(now)
        return [replace(s) for s in due]

    async def started(
        self, schedule_id: str, run_id: str | None, at: datetime, error: str | None
    ) -> None:
        found = self.items.get(schedule_id)
        if found is None:
            return
        if run_id is not None:
            found.last_run_id, found.last_run_at = run_id, at
        found.last_error = error


SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS schedules (
    tenant_id    text NOT NULL DEFAULT 'default',
    schedule_id  text NOT NULL,
    app          text NOT NULL,
    title        text NOT NULL,
    cron         text NOT NULL,
    timezone     text NOT NULL,
    input        jsonb NOT NULL,
    project      jsonb,
    enabled      boolean NOT NULL,
    created_by   text,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    next_run_at  timestamptz,
    last_run_id  text,
    last_run_at  timestamptz,
    last_error   text,
    PRIMARY KEY (tenant_id, schedule_id)
)""",
    "CREATE INDEX IF NOT EXISTS schedules_due ON schedules (next_run_at) WHERE enabled",
)
COLUMNS = (
    "schedule_id, app, title, cron, timezone, input, project, enabled, created_by,"
    " created_at, updated_at, next_run_at, last_run_id, last_run_at, last_error"
)


class PostgresScheduleStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def for_app(self, app: str) -> list[Schedule]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM schedules WHERE tenant_id = %s AND app = %s"
                " ORDER BY created_at",
                (DEFAULT_TENANT, app),
            )
            return [Schedule(**row) for row in await cur.fetchall()]

    async def get(self, schedule_id: str) -> Schedule | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM schedules WHERE tenant_id = %s AND schedule_id = %s",
                (DEFAULT_TENANT, schedule_id),
            )
            row = await cur.fetchone()
        return Schedule(**row) if row else None

    async def put(self, schedule: Schedule) -> None:
        s = schedule
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO schedules (tenant_id, {COLUMNS}) VALUES"
                " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, schedule_id) DO UPDATE SET title = EXCLUDED.title,"
                " cron = EXCLUDED.cron, timezone = EXCLUDED.timezone, input = EXCLUDED.input,"
                " project = EXCLUDED.project, enabled = EXCLUDED.enabled,"
                " updated_at = EXCLUDED.updated_at, next_run_at = EXCLUDED.next_run_at",
                (
                    DEFAULT_TENANT,
                    s.schedule_id,
                    s.app,
                    s.title,
                    s.cron,
                    s.timezone,
                    Jsonb(s.input),
                    Jsonb(s.project) if s.project is not None else None,
                    s.enabled,
                    s.created_by,
                    s.created_at,
                    s.updated_at,
                    s.next_run_at,
                    s.last_run_id,
                    s.last_run_at,
                    s.last_error,
                ),
            )

    async def delete(self, schedule_id: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM schedules WHERE tenant_id = %s AND schedule_id = %s",
                (DEFAULT_TENANT, schedule_id),
            )
            return cur.rowcount > 0

    async def delete_app(self, app: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM schedules WHERE tenant_id = %s AND app = %s", (DEFAULT_TENANT, app)
            )

    async def claim_due(self, now: datetime, limit: int = 20) -> list[Schedule]:
        """The schedules due at ``now``, each moved on to its next time in the same
        transaction: another process checking at the same moment skips them."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM schedules WHERE tenant_id = %s AND enabled"
                " AND next_run_at <= %s ORDER BY next_run_at LIMIT %s FOR UPDATE SKIP LOCKED",
                (DEFAULT_TENANT, now, limit),
            )
            due = [Schedule(**row) for row in await cur.fetchall()]
            for s in due:
                try:
                    s.next_run_at = s.due_after(now)
                except CronError:
                    s.next_run_at = None
                await conn.execute(
                    "UPDATE schedules SET next_run_at = %s"
                    " WHERE tenant_id = %s AND schedule_id = %s",
                    (s.next_run_at, DEFAULT_TENANT, s.schedule_id),
                )
        return due

    async def started(
        self, schedule_id: str, run_id: str | None, at: datetime, error: str | None
    ) -> None:
        async with self._pool.connection() as conn:
            if run_id is not None:
                await conn.execute(
                    "UPDATE schedules SET last_run_id = %s, last_run_at = %s, last_error = %s"
                    " WHERE tenant_id = %s AND schedule_id = %s",
                    (run_id, at, error, DEFAULT_TENANT, schedule_id),
                )
            else:
                await conn.execute(
                    "UPDATE schedules SET last_error = %s"
                    " WHERE tenant_id = %s AND schedule_id = %s",
                    (error, DEFAULT_TENANT, schedule_id),
                )


# --- starting the runs --------------------------------------------------------------

# Starts a run of the schedule (its app's current version, input and project, with the
# schedule as its trigger) and returns its record; raises with a message for people.
Start = Callable[[Schedule], Awaitable[RunRecord]]
# The status of a run, or None when there is no such run.
StatusOf = Callable[[str], Awaitable[str | None]]

UNFINISHED = ("running", "waiting")


def local_text(at: datetime, timezone: str) -> str:
    try:
        return at.astimezone(zone(timezone)).strftime("%Y-%m-%d %H:%M")
    except CronError:
        return at.strftime("%Y-%m-%d %H:%M")


@dataclass
class Scheduler:
    store: ScheduleStore
    start: Start
    status_of: StatusOf
    poll_seconds: float = 30.0
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    async def fire(self, schedule: Schedule, at: datetime, skip_unfinished: bool = True) -> str:
        """Starts the schedule's run now and records it; returns the run's id. A run it
        started before that has not finished yet means this one is skipped (``""``)."""
        if skip_unfinished and schedule.last_run_id:
            if await self.status_of(schedule.last_run_id) in UNFINISHED:
                said = f"上一次运行还没结束，这次跳过（{local_text(at, schedule.timezone)}）"
                await self.store.started(schedule.schedule_id, None, at, said)
                return ""
        try:
            record = await self.start(schedule)
        except Exception as exc:  # noqa: BLE001 - recorded for people, the loop goes on
            said = f"{local_text(at, schedule.timezone)} 没有发起运行：{exc}"
            await self.store.started(schedule.schedule_id, None, at, said[:500])
            return ""
        await self.store.started(schedule.schedule_id, record.run_id, at, None)
        return record.run_id

    async def start_now(self, schedule: Schedule) -> RunRecord:
        """Runs the schedule once now, whatever its last run is doing (立即跑一次); its
        failure goes to the caller and is not recorded."""
        record = await self.start(schedule)
        await self.store.started(schedule.schedule_id, record.run_id, self.clock(), None)
        return record

    async def tick(self) -> int:
        """Starts the runs of the schedules due now; returns how many it started."""
        now = self.clock()
        started = 0
        for schedule in await self.store.claim_due(now):
            if await self.fire(schedule, now):
                started += 1
        return started

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad check must not stop the loop
                log.exception("checking the schedules failed")
            await asyncio.sleep(self.poll_seconds)
