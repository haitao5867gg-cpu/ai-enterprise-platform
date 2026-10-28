"""Starts, inspects and resumes runs.

A run executes in a background task until it finishes or reaches an
``interrupt`` (a human decision). Everything needed to continue is in the
checkpointer, so a run waiting for a decision survives a restart, and runs that
were mid-step when the process stopped are picked up again by ``recover``.
Each of those executions is one segment of the run's trace (see aep.tracing).

A run uses the app version current when it started (or the one it asked for),
for its whole life: the version's configuration is kept with the run and handed
to every execution of its graph (see aep.runtime.appconfig). A draft can be run
too (its eval, or a person trying it); such a run belongs to no version.

There are two kinds of apps. A code app's graph is written in code (registered
in ``aep.runtime.registry``). A flow app's graph is compiled from its
configuration (``aep.runtime.flow``), so each run compiles the configuration it
kept; compiled graphs are cached by configuration. Flow apps are created by
people (``flows``) or shipped with the platform (``BuiltinFlow``, like ``hello``).
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from aep import tracing
from aep.knowledge.store import KnowledgeStore
from aep.runtime import appconfig, design
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow import nodes as flownodes
from aep.runtime.flow.builtin import BuiltinFlow
from aep.runtime.flow.store import (
    FlowAppStore,
    MemoryFlowAppStore,
    MemorySecretStore,
    SecretStore,
)
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


class NoDraftToRun(Exception):
    pass


class InvalidValues(Exception):
    """Values do not fit the fields asked for; ``problems`` by field key."""

    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__("；".join(problems.values()))
        self.problems = problems


@dataclass(frozen=True)
class RunView:
    record: RunRecord
    messages: list[dict[str, str]]
    pending: dict[str, Any] | None
    result: dict[str, Any] | None
    trace_id: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


class RunService:
    def __init__(
        self,
        store: RunStore,
        checkpointer: BaseCheckpointSaver,
        apps: dict[str, AppBuilder | BuiltinFlow],
        ctx: AppContext,
        tracer: tracing.Tracer | None = None,
        versions: AppVersionStore | None = None,
        skills: SkillStore | None = None,
        flows: FlowAppStore | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self._store = store
        self._checkpointer = checkpointer
        self._ctx = ctx
        self._tracer = tracer or tracing.Tracer()
        self.versions = versions or MemoryAppVersionStore()
        self.skills = skills or MemorySkillStore()
        self.flows = flows or MemoryFlowAppStore()
        self.secrets = secrets or MemorySecretStore()
        code = {name: b for name, b in apps.items() if not isinstance(b, BuiltinFlow)}
        self.builtin_flows = {name: b for name, b in apps.items() if isinstance(b, BuiltinFlow)}
        self._graphs: dict[str, CompiledStateGraph] = {
            name: build(ctx).compile(checkpointer=checkpointer) for name, build in code.items()
        }
        self._flow_graphs: dict[str, CompiledStateGraph] = {}
        self._builders = code
        self._designs = {name: design.app_design(build) for name, build in code.items()}
        self.options = {name: appconfig.app_options(build) for name, build in code.items()}
        self.default_model = ctx.settings.default_model
        self._ensured: set[str] = set()
        self._flows_ready = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def tools(self) -> Any:
        """The MCP client nodes call tools with (``aep.tools.mcp.McpTools``)."""
        return self._ctx.tools

    @property
    def knowledge(self) -> KnowledgeStore:
        return self._ctx.knowledge

    @property
    def code_apps(self) -> list[str]:
        return sorted(self._graphs)

    async def ensure_flows(self) -> None:
        """Register the flow apps the platform ships, with their version 1."""
        if self._flows_ready:
            return
        for name, flow in self.builtin_flows.items():
            await self.flows.ensure(name, flow.title, flow.summary)
            await self.versions.ensure(name, flow.config)
        self._flows_ready = True

    async def kind(self, app: str) -> str | None:
        """``code`` or ``flow``; None when there is no such app (or it is archived)."""
        if app in self._graphs:
            return "code"
        await self.ensure_flows()
        found = await self.flows.get(app)
        return "flow" if found is not None and found.archived_at is None else None

    async def app_names(self) -> list[str]:
        await self.ensure_flows()
        return sorted([*self._graphs, *(a.app for a in await self.flows.all())])

    def flow_config(self, app: str, config: dict[str, Any] | None) -> dict[str, Any]:
        """A flow app's configuration as the runtime reads it: a shipped app's
        configuration from before it was a flow app is carried over."""
        shipped = self.builtin_flows.get(app)
        if config is None:
            if shipped is None:
                raise UnknownVersion(f"{app} has no configuration")
            return shipped.config
        return shipped.upgrade(config) if shipped else config

    async def runtime(self, config: dict[str, Any], app: str | None = None) -> flownodes.Runtime:
        """What a flow app's nodes need to run: skill text loaded, constants set, and
        (given the app) its secrets within reach of its tools."""
        skills = {}
        for name, version in flowgraph.skill_refs(config):
            content = await self.skills.version(name, version)
            if content is None:
                raise LookupError(f"Skill {name} 没有版本 {version}")
            skills[(name, version)] = content
        env = {v["key"]: v.get("value") for v in (config.get("env") or {}).get("vars") or []}
        secret = None
        if app is not None:

            async def secret(name: str) -> str | None:
                return await self.secrets.get(app, name)

        return flownodes.Runtime(
            self._ctx.llm_client,
            self.default_model,
            skills,
            env,
            datasources=self._ctx.datasources,
            tools=self._ctx.tools,
            secret=secret,
            sandbox_url=self._ctx.settings.sandbox_url,
            knowledge=self.knowledge,
        )

    async def _graph(self, record: RunRecord) -> CompiledStateGraph:
        if record.app in self._graphs:
            return self._graphs[record.app]
        config = self.flow_config(record.app, record.config)
        # The app is part of the key: its secrets are its own.
        text = json.dumps([record.app, config], sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        if key not in self._flow_graphs:
            built = flowgraph.build(config, await self.runtime(config, record.app))
            self._flow_graphs[key] = built.compile(checkpointer=self._checkpointer)
        return self._flow_graphs[key]

    def builder(self, app: str) -> AppBuilder:
        return self._builders[app]

    def graph(self, app: str) -> CompiledStateGraph:
        return self._graphs[app]

    def app_design(self, app: str) -> design.AppDesign | None:
        return self._designs[app]

    async def ensure_versions(self, app: str) -> None:
        """Give the app its version 1 (its defaults) if it has no versions yet."""
        if app not in self._graphs:
            await self.ensure_flows()
        elif app not in self._ensured:
            await self.versions.ensure(app, appconfig.default_config(self.options[app]))
            self._ensured.add(app)

    async def create(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None = None,
        config: dict[str, Any] | None = None,
        draft: bool = False,
    ) -> RunRecord:
        """Start a run of the app's current version, or of ``app_version``; with ``config``
        (a draft's configuration, for its eval) or ``draft`` (the draft as it is now) the
        run belongs to no version. A flow app's input must fit its start node's inputs."""
        kind = await self.kind(app)
        if kind is None:
            raise UnknownApp(app)
        if draft:
            found_draft = await self.versions.draft(app)
            if found_draft is None:
                raise NoDraftToRun(app)
            config, app_version = found_draft.config, None
        if config is None:
            await self.ensure_versions(app)
            app_version = app_version or await self.versions.current(app)
            found = await self.versions.version(app, app_version)
            if found is None:
                raise UnknownVersion(f"{app} v{app_version}")
            config = found.config
        if kind == "flow":
            config = self.flow_config(app, config)
            _, problems = flownodes.answers(flowgraph.inputs(config), input)
            if problems:
                raise InvalidValues(problems)
        record = await self._store.create(app, input, app_version, config)
        self._spawn(record, {"input": input}, "start", input)
        return record

    async def node_configs(self, record: RunRecord) -> dict[str, appconfig.NodeConfig]:
        """The configuration the run uses, skills loaded (a code app's; a flow app's
        graph has its configuration built in)."""
        if record.app not in self._graphs:
            return {}
        options = self.options[record.app]
        stored = record.config or appconfig.default_config(options)
        return await appconfig.load(stored, options, self.skills)

    async def get(self, run_id: str) -> RunView:
        record = await self._store.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        state = await (await self._graph(record)).aget_state(_config(run_id))
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
            sources=(
                sorted((values.get("sources") or {}).values(), key=lambda hit: hit["index"])
                if record.app not in self._graphs
                else []
            ),
        )

    async def flow(self, run_id: str) -> dict[str, Any]:
        """How the run's app is built and every step this run took (see aep.runtime.design)."""
        record = await self._store.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        flow = await design.flow(
            await self._graph(record),
            _config(run_id),
            {"status": record.status, "input": record.input},
            self._designs.get(record.app),
            self.default_model,
            await self.node_configs(record),
        )
        if record.app in self._graphs:
            for node in flow["nodes"]:
                node.setdefault("type", None)
            for edge in flow["edges"]:
                edge.setdefault("label", None)
            return flow
        config = self.flow_config(record.app, record.config)
        nodes, edges = flowgraph.shape(config, await self.runtime(config))
        app = await self.flows.get(record.app)
        if app is not None:
            flow["design"] = {"title": app.title, "summary": app.summary, **flowgraph.DESIGN}
        by_id = {n["id"]: n for n in flowgraph.nodes(config)}
        for node in nodes:
            settings = by_id.get(node["id"], {})
            if node["kind"] == "model":
                node["model"] = settings.get("model") or self.default_model
            if "skills" in settings:
                node["skills"] = [
                    {"name": s["name"], "version": s["version"]} for s in settings["skills"]
                ]
        return flow | {"nodes": nodes, "edges": edges}

    async def recent(self, limit: int = 50) -> list[RunRecord]:
        return await self._store.recent(limit)

    async def resume(
        self,
        run_id: str,
        decision: str,
        comment: str | None,
        actor: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> RunRecord:
        view = await self.get(run_id)
        if view.record.status != "waiting" or view.pending is None:
            raise NotWaiting(run_id)
        if decision not in view.pending.get("options", []):
            raise InvalidDecision(decision)
        if decision in view.pending.get("comment_required", []) and not (comment or "").strip():
            raise CommentRequired(decision)
        answer: dict[str, Any] = {"decision": decision, "comment": comment, "actor": actor}
        if view.pending.get("kind") == "form":
            checked, problems = flownodes.answers(view.pending.get("fields") or [], values or {})
            if problems:
                raise InvalidValues(problems)
            answer["values"] = checked
        await self._store.set_status(run_id, "running")
        self._spawn(view.record, Command(resume=answer), "resume", answer)
        return await self._store.get(run_id)

    async def recover(self) -> int:
        """Continue runs that were mid-step when the process last stopped."""
        runs = await self._store.list_by_status("running")
        for record in runs:
            state = await (await self._graph(record)).aget_state(_config(record.run_id))
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
        config = _config(record.run_id)
        with self._tracer.segment(record.run_id, record.app, step, step_input) as segment:
            try:
                graph = await self._graph(record)
                nodes = await self.node_configs(record)
                run_config: dict[str, Any] = {
                    "configurable": {**config["configurable"], appconfig.CONFIG_KEY: nodes},
                    "callbacks": segment.callbacks,
                }
                if record.app not in self._graphs:
                    run_config["recursion_limit"] = flowgraph.MAX_STEPS
                try:
                    await graph.ainvoke(payload, run_config)
                except GraphRecursionError as exc:
                    raise RuntimeError(
                        f"运行超过 {flowgraph.MAX_STEPS} 步，已经停止；请检查流程里的循环"
                    ) from exc
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
