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

A run that is running or waiting can be cancelled. Its own process stops it at
once; any process stops it before its next node starts, so a run executing
elsewhere stops after its current node (work already done outside, like tables
written in a dev environment or a job still running on Databricks, stays). A
failed run can be retried from the node that failed when the failure may be
temporary, and any run can be rerun: a new run of the same app version (a draft's
runs copy its configuration), with the same or a changed input.

Every status change says which status it expects to find (``RunStore.set_status``),
so a decision, a cancel and the end of a step cannot overwrite one another.

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
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from aep import tracing
from aep.etl.profile import InputError
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
from aep.runtime.store import RunRecord, RunStatus, RunStore
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


class NotCancellable(Exception):
    """Only a running or waiting run can be cancelled."""


class NotRetryable(Exception):
    """Only a failed run can be retried, and only when its failure may be temporary."""


class NotFailed(NotRetryable):
    """The run is not failed (any more): someone may have retried it already."""


class RunStopped(Exception):
    """The run was cancelled; raised at a node boundary to stop it there."""


# Failures that may go away by themselves: a retry is worth it. The others
# (``output_format``, ``code_error``, ``step_limit``, ``max_visits``, ``max_steps``)
# would happen again; such a run is rerun with a changed input instead. A failure of
# unknown kind may be retried too.
TEMPORARY = frozenset(
    {
        "timeout",
        "model_error",
        "tool_error",
        "knowledge_error",
        "databricks_error",
        "sandbox_unavailable",
    }
)


def retryable(record: RunRecord) -> bool:
    return record.status == "error" and (
        record.error_type is None or record.error_type in TEMPORARY
    )


class StepsExceeded(RuntimeError):
    """A flow app's run took more steps than ``flowgraph.MAX_STEPS``."""


def _error_type(exc: Exception) -> str | None:
    if isinstance(exc, flowgraph.FlowError):
        return exc.type
    if isinstance(exc, StepsExceeded):
        return "max_steps"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, InputError):
        return flownodes.INPUT_ERROR
    return None


class InvalidValues(Exception):
    """Values do not fit the fields asked for; ``problems`` by field key."""

    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__("；".join(problems.values()))
        self.problems = problems


@dataclass(frozen=True)
class RunView:
    record: RunRecord
    messages: list[dict[str, Any]]
    pending: dict[str, Any] | None
    result: dict[str, Any] | None
    trace_id: str | None = None
    # A flow app's numbered sources, each with ``cited`` (see flowgraph.cited).
    sources: list[dict[str, Any]] = field(default_factory=list)


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


TITLE_CHARS = 80


def _short(text: str) -> str:
    line = re.sub(r"\s+", " ", text).strip()
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1] + "…"


class StepTracker(AsyncCallbackHandler):
    """Notes each node of the graph as it starts, as the run's ``step``, and stops the
    run there (``RunStopped``) if it was cancelled meanwhile."""

    # An error raised here stops the graph before the node runs; the node is left to
    # run again if the graph is resumed.
    raise_error = True

    def __init__(self, store: RunStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        metadata = metadata or {}
        node = metadata.get("langgraph_node")
        # Only the graph's own nodes: not what runs inside a node, nor a subgraph's nodes.
        if (
            not isinstance(node, str)
            or node.startswith("__")
            or kwargs.get("name") != node
            or "|" in str(metadata.get("langgraph_checkpoint_ns", ""))
        ):
            return
        design = metadata.get("design")
        title = design.get("title") if isinstance(design, dict) else None
        # The step is noted even when the run stops here: it says where it stopped, as
        # the flow view does.
        try:
            await self._store.set_step(
                self._run_id, node, title if isinstance(title, str) else node
            )
            record = await self._store.get(self._run_id)
        except Exception:  # noting the step must not stop the run
            log.exception("could not note the step of run %s", self._run_id)
            return
        if record is not None and record.status == "cancelled":
            raise RunStopped(self._run_id)


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
        # The task executing each run in this process, to stop it when it is cancelled.
        self._running: dict[str, asyncio.Task] = {}

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
            settings=self._ctx.settings,
            databricks=self._ctx.databricks,
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
        requester: str | None = None,
        rerun_of: str | None = None,
    ) -> RunRecord:
        """Start a run of the app's current version, or of ``app_version``; with ``config``
        (a draft's configuration, for its eval) or ``draft`` (the draft as it is now) the
        run belongs to no version. A flow app's input must fit its start node's inputs.
        ``requester`` is who asks for it, as they say."""
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
        record = await self._store.create(app, input, app_version, config, requester, rerun_of)
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
        sources: list[dict[str, Any]] = []
        if record.app not in self._graphs:
            found = sorted((values.get("sources") or {}).values(), key=lambda hit: hit["index"])
            if found:
                # Worked out each time the run is read, so runs from before it have it too.
                show = ((pending or {}).get("details") or {}).get("show")
                used = flowgraph.cited(
                    self.flow_config(record.app, record.config),
                    values,
                    [show] if isinstance(show, str) else [],
                )
                sources = [{**hit, "cited": hit["index"] in used} for hit in found]
        return RunView(
            record=record,
            messages=values.get("messages", []),
            pending=pending,
            result=values.get("result"),
            trace_id=tracing.trace_id(run_id) if self._tracer.enabled else None,
            sources=sources,
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
            # Only the app's own description: flowgraph.DESIGN is the same builder text for
            # every flow app and stays on the app's page.
            flow["design"] = {
                "title": app.title,
                "summary": app.summary,
                "pattern": "",
                "knowledge": "",
                "skills": "",
            }
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

    async def app_titles(self) -> dict[str, str]:
        """Every app's title by name, archived flow apps included (their runs remain)."""
        await self.ensure_flows()
        titles = {name: d.title if d else name for name, d in self._designs.items()}
        return titles | {a.app: a.title for a in await self.flows.all(archived=True)}

    def inputs(self, record: RunRecord) -> list[dict[str, Any]] | None:
        """What the configuration a flow app's run used asks for (its start node's
        inputs), which may differ from the app's current version; None for a code app."""
        if record.app in self._graphs:
            return None
        try:
            return flowgraph.inputs(self.flow_config(record.app, record.config))
        except Exception:  # a configuration the runtime no longer reads
            return []

    async def copy_inputs(self, record: RunRecord) -> list[dict[str, Any]] | None:
        """What a copy of the run asks for: the start inputs of the configuration
        ``rerun`` uses. That is the run's own, except for a run from before versions,
        which is copied into the current version."""
        if record.app_version is not None or record.config is not None:
            return self.inputs(record)
        if record.app in self._graphs:
            return None
        try:
            await self.ensure_versions(record.app)
            found = await self.versions.version(record.app, await self.versions.current(record.app))
            return flowgraph.inputs(self.flow_config(record.app, found and found.config))
        except Exception:  # a configuration the runtime no longer reads
            return []

    def title(self, record: RunRecord) -> str | None:
        """What the run is about, in one line: a flow app's first text input that was
        filled in (in the start node's order), a code app's ``request``."""
        fields = self.inputs(record)
        if fields is None:
            keys = ["request"]
        else:
            keys = [f["key"] for f in fields if f.get("type") in ("text", "textarea")]
        for key in keys:
            value = record.input.get(key)
            if isinstance(value, str) and value.strip():
                return _short(value)
        return None

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
        # Only one of two decisions sent at once, or a decision and a cancel, gets here.
        if not await self._store.set_status(run_id, "running", expected=("waiting",)):
            raise NotWaiting(run_id)
        self._spawn(view.record, Command(resume=answer), "resume", answer)
        return await self._store.get(run_id)

    async def cancel(self, run_id: str, actor: str | None, reason: str | None) -> RunRecord:
        """Cancel a running or waiting run; ``reason`` is kept as its ``error``."""
        record = await self._store.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        if not await self._store.set_status(
            run_id, "cancelled", reason, expected=("running", "waiting"), actor=actor
        ):
            raise NotCancellable(run_id)
        task = self._running.get(run_id)
        if task is not None:
            task.cancel()
        return await self._store.get(run_id)

    async def retry(self, run_id: str) -> RunRecord:
        """Continue a failed run from the node that failed (from its input if it failed
        before its first step was saved)."""
        record = await self._store.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        if record.status != "error":
            raise NotFailed(run_id)
        if not retryable(record):
            raise NotRetryable(run_id)
        state = await (await self._graph(record)).aget_state(_config(run_id))
        if not await self._store.set_status(run_id, "running", expected=("error",)):
            raise NotFailed(run_id)
        self._spawn(record, None if state.values else {"input": record.input}, "retry")
        return await self._store.get(run_id)

    async def rerun(
        self,
        run_id: str,
        input: dict[str, Any] | None = None,
        requester: str | None = None,
    ) -> RunRecord:
        """A new run of the same app version as this one, with its input or ``input``.
        A draft's run copies the configuration it ran with; a run from before versions
        uses the current version."""
        source = await self._store.get(run_id)
        if source is None:
            raise RunNotFound(run_id)
        draft = source.app_version is None and source.config is not None
        return await self.create(
            source.app,
            source.input if input is None else input,
            source.app_version,
            config=source.config if draft else None,
            requester=requester,
            rerun_of=source.run_id,
        )

    async def can_rerun(self, record: RunRecord) -> bool:
        if await self.kind(record.app) is None:
            return False
        if record.app_version is None:
            return True
        await self.ensure_versions(record.app)
        return await self.versions.version(record.app, record.app_version) is not None

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
        self._running[record.run_id] = task

        def done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if self._running.get(record.run_id) is finished:
                del self._running[record.run_id]

        task.add_done_callback(done)

    async def _finish(self, run_id: str, status: RunStatus, **kwargs: Any) -> None:
        # Only a run still running ends this way: a cancelled one stays cancelled.
        await self._store.set_status(run_id, status, expected=("running",), **kwargs)

    async def _drive(self, record: RunRecord, payload: Any, step: str, step_input: Any) -> None:
        config = _config(record.run_id)
        with self._tracer.segment(record.run_id, record.app, step, step_input) as segment:
            try:
                graph = await self._graph(record)
                nodes = await self.node_configs(record)
                run_config: dict[str, Any] = {
                    "configurable": {
                        **config["configurable"],
                        appconfig.CONFIG_KEY: nodes,
                        # Who asked for the run: a flow app's start message is theirs.
                        "requester": record.created_by,
                    },
                    "callbacks": [*segment.callbacks, StepTracker(self._store, record.run_id)],
                }
                if record.app not in self._graphs:
                    run_config["recursion_limit"] = flowgraph.MAX_STEPS
                try:
                    await graph.ainvoke(payload, run_config)
                except GraphRecursionError as exc:
                    raise StepsExceeded(
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
                await self._finish(record.run_id, status)
            except (RunStopped, asyncio.CancelledError) as exc:
                # Cancelled (its status says so already), or this process is stopping:
                # then the run is still running and ``recover`` picks it up again.
                segment.finish({"status": "stopped"})
                if isinstance(exc, asyncio.CancelledError):
                    raise
            except Exception as exc:
                log.exception("run %s failed", record.run_id)
                segment.finish({"status": "error"}, error=str(exc))
                await self._finish(
                    record.run_id, "error", error=str(exc), error_type=_error_type(exc)
                )
