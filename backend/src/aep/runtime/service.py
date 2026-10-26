"""Starts, inspects and resumes runs.

A run executes in a background task until it finishes or reaches an
``interrupt`` (a human decision). Everything needed to continue is in the
checkpointer, so a run waiting for a decision survives a restart, and runs that
were mid-step when the process stopped are picked up again by ``recover``.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from aep.runtime.registry import AppBuilder, AppContext
from aep.runtime.store import RunRecord, RunStore

log = logging.getLogger(__name__)


class UnknownApp(Exception):
    pass


class RunNotFound(Exception):
    pass


class NotWaiting(Exception):
    pass


class InvalidDecision(Exception):
    pass


@dataclass(frozen=True)
class RunView:
    record: RunRecord
    messages: list[dict[str, str]]
    pending: dict[str, Any] | None
    result: dict[str, Any] | None


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


class RunService:
    def __init__(
        self,
        store: RunStore,
        checkpointer: BaseCheckpointSaver,
        apps: dict[str, AppBuilder],
        ctx: AppContext,
    ) -> None:
        self._store = store
        self._graphs: dict[str, CompiledStateGraph] = {
            name: build(ctx).compile(checkpointer=checkpointer) for name, build in apps.items()
        }
        self._tasks: set[asyncio.Task] = set()

    @property
    def apps(self) -> list[str]:
        return sorted(self._graphs)

    async def create(self, app: str, input: dict[str, Any]) -> RunRecord:
        if app not in self._graphs:
            raise UnknownApp(app)
        record = await self._store.create(app, input)
        self._spawn(record, {"input": input})
        return record

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
        )

    async def recent(self, limit: int = 50) -> list[RunRecord]:
        return await self._store.recent(limit)

    async def resume(self, run_id: str, decision: str, comment: str | None) -> RunRecord:
        view = await self.get(run_id)
        if view.record.status != "waiting" or view.pending is None:
            raise NotWaiting(run_id)
        if decision not in view.pending.get("options", []):
            raise InvalidDecision(decision)
        await self._store.set_status(run_id, "running")
        self._spawn(view.record, Command(resume={"decision": decision, "comment": comment}))
        return await self._store.get(run_id)

    async def recover(self) -> int:
        """Continue runs that were mid-step when the process last stopped."""
        runs = await self._store.list_by_status("running")
        for record in runs:
            state = await self._graphs[record.app].aget_state(_config(record.run_id))
            # No checkpoint yet means the run never got going: start it from its input.
            payload = None if state.values else {"input": record.input}
            self._spawn(record, payload)
        return len(runs)

    async def wait_idle(self) -> None:
        """Wait for every background step started so far (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, record: RunRecord, payload: Any) -> None:
        task = asyncio.create_task(self._drive(record, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drive(self, record: RunRecord, payload: Any) -> None:
        graph = self._graphs[record.app]
        config = _config(record.run_id)
        try:
            await graph.ainvoke(payload, config)
            state = await graph.aget_state(config)
            await self._store.set_status(record.run_id, "waiting" if state.interrupts else "done")
        except Exception as exc:
            log.exception("run %s failed", record.run_id)
            await self._store.set_status(record.run_id, "error", error=str(exc))
