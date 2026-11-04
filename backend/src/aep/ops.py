"""Operations dashboard, compute usage and per-app model budgets (docs/api/ops.md).

The dashboard only reads what the platform already records (runs, model calls, the asset
catalog) plus the Databricks job time data nodes report here. Budgets are a platform
policy: ``BudgetGuard`` is installed as ``aep.llm``'s guard and checked before every model
call made from a run, so apps never handle them."""

import logging
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from aep import llm
from aep.compute import ComputeRecord
from aep.models import CallRollup, CallStore, SettingStore
from aep.runtime.store import DEFAULT_TENANT, FINISHED, RunRecord, RunStat, RunStore

if TYPE_CHECKING:
    from aep.notify import Notifier

log = logging.getLogger(__name__)

DAYS = (7, 30, 90)
PRICE_KEY = "ops.compute_hourly_usd"
CACHE_SECONDS = 30.0
MAX_PRICE = 1000.0
MAX_BUDGET = 100000.0


class ComputeStore(Protocol):
    async def setup(self) -> None: ...
    async def record(self, record: ComputeRecord) -> None: ...
    async def by_run(self, since: datetime) -> dict[str | None, float]: ...


class MemoryComputeStore:
    def __init__(self) -> None:
        self.records: list[tuple[datetime, ComputeRecord]] = []

    async def setup(self) -> None:
        return None

    async def record(self, record: ComputeRecord) -> None:
        self.records.append((datetime.now(UTC), record))

    async def by_run(self, since: datetime) -> dict[str | None, float]:
        found: dict[str | None, float] = {}
        for at, record in self.records:
            if at >= since:
                found[record.run_id] = found.get(record.run_id, 0.0) + record.seconds
        return found


# Budgets


@dataclass(frozen=True)
class Budget:
    app: str
    monthly_usd: float
    warn_at: float = 0.8
    block: bool = True
    updated_by: str | None = None
    updated_at: datetime | None = None
    # The months ("2026-11") a warning and a used-up notice already went out for.
    warned_month: str | None = None
    over_month: str | None = None


class BudgetStore(Protocol):
    async def setup(self) -> None: ...
    async def get(self, app: str) -> Budget | None: ...
    async def all(self) -> list[Budget]: ...
    async def put(self, budget: Budget) -> Budget: ...
    async def delete(self, app: str) -> None: ...
    async def mark(self, app: str, **months: str) -> None: ...


class MemoryBudgetStore:
    def __init__(self) -> None:
        self.budgets: dict[str, Budget] = {}

    async def setup(self) -> None:
        return None

    async def get(self, app: str) -> Budget | None:
        return self.budgets.get(app)

    async def all(self) -> list[Budget]:
        return sorted(self.budgets.values(), key=lambda b: b.app)

    async def put(self, budget: Budget) -> Budget:
        old = self.budgets.get(budget.app)
        same = old is not None and (old.monthly_usd, old.warn_at) == (
            budget.monthly_usd,
            budget.warn_at,
        )
        budget = replace(
            budget,
            updated_at=datetime.now(UTC),
            warned_month=old.warned_month if old and same else None,
            over_month=old.over_month if old and same else None,
        )
        self.budgets[budget.app] = budget
        return budget

    async def delete(self, app: str) -> None:
        self.budgets.pop(app, None)

    async def mark(self, app: str, **months: str) -> None:
        if app in self.budgets:
            self.budgets[app] = replace(self.budgets[app], **months)


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS compute_usage (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL DEFAULT 'default',
    run_id      text,
    node        text,
    kind        text NOT NULL,
    cluster_id  text,
    seconds     double precision NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
)""",
    "CREATE INDEX IF NOT EXISTS compute_usage_at ON compute_usage (tenant_id, at)",
    """
CREATE TABLE IF NOT EXISTS app_budgets (
    tenant_id    text NOT NULL DEFAULT 'default',
    app          text NOT NULL,
    monthly_usd  double precision NOT NULL,
    warn_at      double precision NOT NULL DEFAULT 0.8,
    block        boolean NOT NULL DEFAULT true,
    updated_by   text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    warned_month text,
    over_month   text,
    PRIMARY KEY (tenant_id, app)
)""",
]
BUDGET_COLUMNS = (
    "app, monthly_usd, warn_at, block, updated_by, updated_at, warned_month, over_month"
)


# A new amount or warning threshold starts this month's notices over.
SAME = "app_budgets.monthly_usd = excluded.monthly_usd AND app_budgets.warn_at = excluded.warn_at"


class PostgresOpsStore:
    """Compute usage and budgets in Postgres (dict rows, autocommit)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def record(self, record: ComputeRecord) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO compute_usage (run_id, node, kind, cluster_id, seconds)"
                " VALUES (%s, %s, %s, %s, %s)",
                (record.run_id, record.node, record.kind, record.cluster_id, record.seconds),
            )

    async def by_run(self, since: datetime) -> dict[str | None, float]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, sum(seconds) AS seconds FROM compute_usage"
                " WHERE tenant_id = %s AND at >= %s GROUP BY run_id",
                (DEFAULT_TENANT, since),
            )
            return {row["run_id"]: float(row["seconds"]) for row in await cur.fetchall()}

    async def get(self, app: str) -> Budget | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {BUDGET_COLUMNS} FROM app_budgets WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            row = await cur.fetchone()
            return Budget(**row) if row else None

    async def all(self) -> list[Budget]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {BUDGET_COLUMNS} FROM app_budgets WHERE tenant_id = %s ORDER BY app",
                (DEFAULT_TENANT,),
            )
            return [Budget(**row) for row in await cur.fetchall()]

    async def put(self, budget: Budget) -> Budget:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO app_budgets (app, monthly_usd, warn_at, block, updated_by)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, app) DO UPDATE SET"
                " monthly_usd = excluded.monthly_usd, warn_at = excluded.warn_at,"
                " block = excluded.block, updated_by = excluded.updated_by,"
                f" warned_month = CASE WHEN {SAME} THEN app_budgets.warned_month END,"
                f" over_month = CASE WHEN {SAME} THEN app_budgets.over_month END,"
                f" updated_at = now() RETURNING {BUDGET_COLUMNS}",
                (budget.app, budget.monthly_usd, budget.warn_at, budget.block, budget.updated_by),
            )
            return Budget(**await cur.fetchone())

    async def delete(self, app: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM app_budgets WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )

    async def mark(self, app: str, **months: str) -> None:
        if months.keys() - {"warned_month", "over_month"}:
            raise ValueError("unknown budget mark")
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE app_budgets SET "
                + ", ".join(f"{key} = %s" for key in months)
                + " WHERE tenant_id = %s AND app = %s",
                (*months.values(), DEFAULT_TENANT, app),
            )


# The dashboard


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


@dataclass
class _Metrics:
    runs: list[RunStat]
    calls: int = 0
    unpriced: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    compute_seconds: float = 0.0

    def add_calls(self, rollup: CallRollup) -> None:
        self.calls += rollup.calls
        self.unpriced += rollup.unpriced
        self.prompt_tokens += rollup.prompt_tokens
        self.completion_tokens += rollup.completion_tokens
        if rollup.cost_usd is not None:
            self.cost = (self.cost or 0.0) + rollup.cost_usd

    def out(self, price: float | None) -> dict[str, Any]:
        runs = self.runs
        done = [r for r in runs if r.status == "done"]
        firsts = [r for r in runs if r.rerun_of is None and r.status in FINISHED]
        passed = [
            r for r in firsts if r.status == "done" and r.outcome != "stopped" and not r.reran
        ]
        return {
            "runs": len(runs),
            "done": len(done),
            "error": sum(r.status == "error" for r in runs),
            "cancelled": sum(r.status == "cancelled" for r in runs),
            "active": sum(r.status not in FINISHED for r in runs),
            "delivery_seconds_median": _median(
                [(r.updated_at - r.created_at).total_seconds() for r in done]
            ),
            "wait_seconds_median": _median(
                [r.wait_seconds for r in done if r.wait_seconds is not None]
            ),
            "first_pass_rate": round(len(passed) / len(firsts), 4) if firsts else None,
            "model_cost_usd": round(self.cost, 6) if self.cost is not None else None,
            "model_calls": self.calls,
            "unpriced_calls": self.unpriced,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "compute_seconds": round(self.compute_seconds, 1),
            "compute_cost_usd": (
                round(self.compute_seconds / 3600 * price, 4) if price is not None else None
            ),
        }


Titles = Callable[[], Awaitable[dict[str, str]]]
Reuse = Callable[[], Awaitable[dict[str, Any]]]


class Ops:
    def __init__(
        self,
        runs: RunStore,
        calls: CallStore,
        compute: ComputeStore,
        budgets: BudgetStore,
        settings_store: SettingStore,
        titles: Titles,
        *,
        timezone: str = "Asia/Shanghai",
        reuse: Reuse | None = None,
        notifier: "Notifier | None" = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runs = runs
        self.calls = calls
        self.compute = compute
        self.budgets = budgets
        self.settings_store = settings_store
        self.titles = titles
        self.zone = ZoneInfo(timezone)
        self.timezone = timezone
        self.reuse = reuse
        self.notifier = notifier
        self.now = now
        self.clock = clock
        self._run_apps: dict[str, str | None] = {}
        self._cache: dict[str, tuple[float, Any]] = {}

    # Settings

    async def price(self) -> float | None:
        value = await self.settings_store.get(PRICE_KEY)
        return float(value) if isinstance(value, int | float) else None

    async def set_price(self, value: float | None) -> float | None:
        await self.settings_store.set(PRICE_KEY, value)
        return value

    # Months and costs

    def month(self) -> str:
        return self.now().astimezone(self.zone).strftime("%Y-%m")

    def _month_start(self) -> datetime:
        local = self.now().astimezone(self.zone)
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def _apps(self, run_ids: list[str]) -> dict[str, str]:
        unknown = [key for key in dict.fromkeys(run_ids) if key not in self._run_apps]
        if unknown:
            found = await self.runs.apps_of(unknown)
            if len(self._run_apps) > 20000:
                self._run_apps.clear()
            for key in unknown:
                self._run_apps[key] = found.get(key)
        return {key: app for key in run_ids if (app := self._run_apps.get(key))}

    async def month_costs(self) -> dict[str, float]:
        """What each app's runs cost this month (calls the gateway priced)."""
        rollups = await self.calls.rollup(self._month_start(), self.timezone)
        apps = await self._apps([r.run_id for r in rollups if r.run_id])
        costs: dict[str, float] = {}
        for rollup in rollups:
            app = apps.get(rollup.run_id or "")
            if app and rollup.cost_usd is not None:
                costs[app] = costs.get(app, 0.0) + rollup.cost_usd
        return costs

    async def _cached(self, key: str, load: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = await load()
        self._cache[key] = (self.clock(), value)
        return value

    def _forget(self) -> None:
        self._cache.clear()

    def budget_out(self, budget: Budget | None, spent: float) -> dict[str, Any] | None:
        if budget is None:
            return None
        state = "ok"
        if spent >= budget.monthly_usd:
            state = "over"
        elif spent >= budget.monthly_usd * budget.warn_at:
            state = "warn"
        return {
            "monthly_usd": budget.monthly_usd,
            "warn_at": budget.warn_at,
            "block": budget.block,
            "month_cost_usd": round(spent, 6),
            "state": state,
            "updated_by": budget.updated_by,
            "updated_at": budget.updated_at,
        }

    async def budget(self, app: str) -> dict[str, Any] | None:
        budget = await self.budgets.get(app)
        if budget is None:
            return None
        return self.budget_out(budget, (await self.month_costs()).get(app, 0.0))

    async def set_budget(
        self, app: str, monthly_usd: float, warn_at: float, block: bool, actor: str | None
    ) -> dict[str, Any] | None:
        await self.budgets.put(Budget(app, monthly_usd, warn_at, block, actor))
        self._forget()
        return await self.budget(app)

    async def delete_budget(self, app: str) -> None:
        await self.budgets.delete(app)
        self._forget()

    # The guard

    async def guard(self, run_id: str | None) -> None:
        """Stops a model call from a run whose app used up a blocking budget; sends the
        month's warning and used-up notices. Spending is cached for ``CACHE_SECONDS``."""
        if not run_id:
            return
        budgets: dict[str, Budget] = await self._cached("budgets", lambda: self._budget_map())
        if not budgets:
            return
        app = (await self._apps([run_id])).get(run_id)
        budget = budgets.get(app or "")
        if budget is None or app is None:
            return
        spent = (await self._cached("costs", self.month_costs)).get(app, 0.0)
        await self._notify(budget, spent, run_id)
        if budget.block and spent >= budget.monthly_usd:
            title = (await self.titles()).get(app, app)
            raise llm.BudgetExceeded(
                f"应用「{title}」本月的模型预算 ${budget.monthly_usd:g} 已经用完，"
                "请管理员在运营看板调高预算或下月再用"
            )

    async def _budget_map(self) -> dict[str, Budget]:
        return {b.app: b for b in await self.budgets.all()}

    async def _notify(self, budget: Budget, spent: float, run_id: str) -> None:
        month = self.month()
        over = spent >= budget.monthly_usd
        warn = spent >= budget.monthly_usd * budget.warn_at
        field = (
            "over_month"
            if over and budget.over_month != month
            else "warned_month"
            if warn and not over and budget.warned_month != month
            else None
        )
        if field is None:
            return
        # Marked first, in the cached copy too, so one crossing sends one notice.
        await self.budgets.mark(budget.app, **{field: month})
        budgets = self._cache.get("budgets")
        if budgets:
            budgets[1][budget.app] = replace(budget, **{field: month})
        if self.notifier is None:
            return
        title = (await self.titles()).get(budget.app, budget.app)
        try:
            await self.notifier.budget(
                budget.app,
                title,
                run_id,
                over=over,
                spent=spent,
                monthly=budget.monthly_usd,
                block=budget.block,
            )
        except Exception:  # noqa: BLE001 - a notice must not stop the run
            log.exception("could not send the budget notice for %s", budget.app)

    # The summary

    async def summary(self, days: int) -> dict[str, Any]:
        now = self.now()
        since = now - timedelta(days=days)
        stats = await self.runs.stats(since)
        rollups = await self.calls.rollup(since, self.timezone)
        compute = await self.compute.by_run(since)
        price = await self.price()
        titles = await self.titles()
        budgets = {b.app: b for b in await self.budgets.all()}
        month_costs = await self.month_costs() if budgets else {}
        run_apps = {s.run_id: s.app for s in stats}
        missing = [
            key for key in [*(r.run_id for r in rollups), *compute] if key and key not in run_apps
        ]
        run_apps |= await self._apps(missing)

        totals = _Metrics(list(stats))
        per_app: dict[str, _Metrics] = {}
        for stat in stats:
            per_app.setdefault(stat.app, _Metrics([])).runs.append(stat)
        for rollup in rollups:
            totals.add_calls(rollup)
            app = run_apps.get(rollup.run_id or "")
            if app:
                per_app.setdefault(app, _Metrics([])).add_calls(rollup)
        for run_id, seconds in compute.items():
            totals.compute_seconds += seconds
            app = run_apps.get(run_id or "")
            if app:
                per_app.setdefault(app, _Metrics([])).compute_seconds += seconds
        for app in budgets:
            per_app.setdefault(app, _Metrics([]))

        apps = [
            {
                "app": app,
                "title": titles.get(app, app),
                **metrics.out(price),
                "budget": self.budget_out(budgets.get(app), month_costs.get(app, 0.0)),
            }
            for app, metrics in per_app.items()
        ]
        apps.sort(key=lambda a: (-a["runs"], a["title"]))
        return {
            "days": days,
            "since": since,
            "totals": totals.out(price),
            "apps": apps,
            "daily": self._daily(stats, rollups, now, days),
            "reuse": await self.reuse() if self.reuse else None,
            "compute_hourly_usd": price,
        }

    async def app_metrics(self, records: list[RunRecord], days: int) -> dict[str, Any]:
        """``since``, ``totals`` and ``daily`` as the summary has them, for these runs of
        one app (docs/api/ops.md, "应用的使用情况")."""
        now = self.now()
        since = now - timedelta(days=days)
        copied = {r.rerun_of for r in records if r.rerun_of}
        stats = [
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
            for r in records
        ]
        ids = {r.run_id for r in records}
        rollups = [r for r in await self.calls.rollup(since, self.timezone) if r.run_id in ids]
        metrics = _Metrics(stats)
        for rollup in rollups:
            metrics.add_calls(rollup)
        for run_id, seconds in (await self.compute.by_run(since)).items():
            if run_id in ids:
                metrics.compute_seconds += seconds
        return {
            "since": since,
            "totals": metrics.out(await self.price()),
            "daily": self._daily(stats, rollups, now, days),
        }

    def _daily(
        self, stats: list[RunStat], rollups: list[CallRollup], now: datetime, days: int
    ) -> list[dict[str, Any]]:
        today = now.astimezone(self.zone).date()
        dates = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
        rows: dict[date, dict[str, Any]] = {
            d: {
                "date": d.isoformat(),
                "runs": 0,
                "done": 0,
                "error": 0,
                "model_cost_usd": None,
            }
            for d in dates
        }
        for stat in stats:
            row = rows.get(stat.created_at.astimezone(self.zone).date())
            if row:
                row["runs"] += 1
                row["done"] += stat.status == "done"
                row["error"] += stat.status == "error"
        for rollup in rollups:
            row = rows.get(rollup.day)
            if row and rollup.cost_usd is not None:
                row["model_cost_usd"] = round((row["model_cost_usd"] or 0.0) + rollup.cost_usd, 6)
        return list(rows.values())


def reuse_from(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Asset reuse from the asset catalog's items (aep.asset_catalog)."""

    def users(item: dict[str, Any]) -> int:
        return len({use["app"] for use in item["used_by"]})

    assets = [item for item in items if item["kind"] != "template"]
    top = sorted(
        (item for item in assets if users(item) > 0),
        key=lambda item: (-users(item), item["title"]),
    )[:5]
    return {
        "assets": len(assets),
        "reused": sum(users(item) >= 2 for item in assets),
        "apps_from_templates": sum(users(item) for item in items if item["kind"] == "template"),
        "top": [
            {
                "kind": item["kind"],
                "id": item["id"],
                "title": item["title"],
                "link": item["link"],
                "used_by": users(item),
            }
            for item in top
        ],
    }
