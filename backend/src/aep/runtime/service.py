"""Starts, inspects and resumes runs.

A run executes in a background task until it finishes or reaches an
``interrupt`` (a human decision). Everything needed to continue is in the
checkpointer, so a run waiting for a decision survives a restart, and runs that
were mid-step when the process stopped are picked up again by ``recover``.
Each of those executions is one segment of the run's trace (see aep.tracing).

A run uses the app version current when it started (or the one it asked for),
for its whole life: the version's configuration is kept with the run and handed
to every execution of its graph (see aep.runtime.appconfig).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from aep import tracing
from aep.runtime import appconfig, design
from aep.runtime.registry import AppBuilder, AppContext
from aep.runtime.store import RunRecord, RunStore
from aep.runtime.versions import AppVersionStore, MemoryAppVersionStore
from aep.skills import MemorySkillStore, SkillStore

log = logging.getLogger(__name__)


class UnknownApp(Exception):
    pass


class RunNotFound(Exception):
    pass


class NotWaiting(Exception):
    pass


class InvalidDecision(Exception):
    pass


class CommentRequired(Exception):
    pass


class UnknownVersion(Exception):
    pass


@dataclass(frozen=True)
class RunView:
    record: RunRecord
    messages: list[dict[str, str]]
    pending: dict[str, Any] | None
    result: dict[str, Any] | None
    trace_id: str | None = None


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


class RunService:
    def __init__(
        self,
        store: RunStore,
        checkpointer: BaseCheckpointSaver,
        apps: dict[str, AppBuilder],
        ctx: AppContext,
        tracer: tracing.Tracer | None = None,
        versions: AppVersionStore | None = None,
        skills: SkillStore | None = None,
    ) -> None:
        self._store = store
        self._tracer = tracer or tracing.Tracer()
        self.versions = versions or MemoryAppVersionStore()
        self.skills = skills or MemorySkillStore()
        self._graphs: dict[str, CompiledStateGraph] = {
            name: build(ctx).compile(checkpointer=checkpointer) for name, build in apps.items()
        }
        self._builders = dict(apps)
        self._designs = {name: design.app_design(build) for name, build in apps.items()}
        self.options = {name: appconfig.app_options(build) for name, build in apps.items()}
        self.default_model = ctx.settings.default_model
        self._ensured: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def apps(self) -> list[str]:
        return sorted(self._graphs)

    def builder(self, app: str) -> AppBuilder:
        return self._builders[app]

    def graph(self, app: str) -> CompiledStateGraph:
        return self._graphs[app]

    def app_design(self, app: str) -> design.AppDesign | None:
        return self._designs[app]

    async def ensure_versions(self, app: str) -> None:
        """Give the app its version 1 (its defaults) if it has no versions yet."""
        if app not in self._ensured:
            await self.versions.ensure(app, appconfig.default_config(self.options[app]))
            self._ensured.add(app)

    async def create(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Start a run of the app's current version, or of ``app_version``; with ``config``
        (a draft's configuration, for its eval) the run belongs to no version."""
        if app not in self._graphs:
            raise UnknownApp(app)
        if config is None:
            await self.ensure_versions(app)
            app_version = app_version or await self.versions.current(app)
            found = await self.versions.version(app, app_version)
            if found is None:
                raise UnknownVersion(f"{app} v{app_version}")
            config = found.config
        record = await self._store.create(app, input, app_version, config)
        self._spawn(record, {"input": input}, "start", input)
        return record

    async def node_configs(self, record: RunRecord) -> dict[str, appconfig.NodeConfig]:
        """The configuration the run uses, skills loaded."""
        options = self.options[record.app]
        stored = record.config or appconfig.default_config(options)
        return await appconfig.load(stored, options, self.skills)

    async def get(self, run_id: str) -> RunView:
        record = await self._store.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        state = await self._graphs[record.app].aget_state(_config(run_id))
        values = state.values or {}
        pending = (
            state.interrupts[0].value if record.status == "waiting" and state.interrupts else None
        )
        return RunView(
            record=record,
            messages=values.get("messages", []),
            pending=pending,
            result=values.get("result"),
            trace_id=tracing.trace_id(run_id) if self._tracer.enabled else None,
        )

    async def flow(self, run_id: str) -> dict[str, Any]:
        """How the run's app is built and every step this run took (see aep.runtime.design)."""
        record = await self._store.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        return await design.flow(
            self._graphs[record.app],
            _config(run_id),
            {"status": record.status, "input": record.input},
            self._designs[record.app],
            self.default_model,
            await self.node_configs(record),
        )

    async def recent(self, limit: int = 50) -> list[RunRecord]:
        return await self._store.recent(limit)

    async def resume(
        self, run_id: str, decision: str, comment: str | None, actor: str | None = None
    ) -> RunRecord:
        view = await self.get(run_id)
        if view.record.status != "waiting" or view.pending is None:
            raise NotWaiting(run_id)
        if decision not in view.pending.get("options", []):
            raise InvalidDecision(decision)
        if decision in view.pending.get("comment_required", []) and not (comment or "").strip():
            raise CommentRequired(decision)
        await self._store.set_status(run_id, "running")
        answer = {"decision": decision, "comment": comment, "actor": actor}
        self._spawn(view.record, Command(resume=answer), "resume", answer)
        return await self._store.get(run_id)

    async def recover(self) -> int:
        """Continue runs that were mid-step when the process last stopped."""
        runs = await self._store.list_by_status("running")
        for record in runs:
            state = await self._graphs[record.app].aget_state(_config(record.run_id))
            # No checkpoint yet means the run never got going: start it from its input.
            payload = None if state.values else {"input": record.input}
            self._spawn(record, payload, "recover")
        return len(runs)

    async def wait_idle(self) -> None:
        """Wait for every background step started so far (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, record: RunRecord, payload: Any, step: str, step_input: Any = None) -> None:
        task = asyncio.create_task(self._drive(record, payload, step, step_input))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drive(self, record: RunRecord, payload: Any, step: str, step_input: Any) -> None:
        graph = self._graphs[record.app]
        config = _config(record.run_id)
        with self._tracer.segment(record.run_id, record.app, step, step_input) as segment:
            try:
                nodes = await self.node_configs(record)
                run_config = {
                    "configurable": {**config["configurable"], appconfig.CONFIG_KEY: nodes},
                    "callbacks": segment.callbacks,
                }
                await graph.ainvoke(payload, run_config)
                state = await graph.aget_state(config)
                status = "waiting" if state.interrupts else "done"
                segment.finish(
                    {
                        "status": status,
                        "pending": state.interrupts[0].value if state.interrupts else None,
                        "result": (state.values or {}).get("result"),
                    }
                )
                await self._store.set_status(record.run_id, status)
            except Exception as exc:
                log.exception("run %s failed", record.run_id)
                segment.finish({"status": "error"}, error=str(exc))
                await self._store.set_status(record.run_id, "error", error=str(exc))
