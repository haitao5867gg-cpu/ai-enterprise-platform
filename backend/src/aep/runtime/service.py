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
import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from aep import policy, projects, tracing
from aep.etl.profile import InputError
from aep.knowledge.store import KnowledgeStore
from aep.runtime import appconfig, design, human
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow import nodes as flownodes
from aep.runtime.flow import refs
from aep.runtime.flow.builtin import BuiltinFlow
from aep.runtime.flow.check import NOT_SETTINGS
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
    """The decision is not one of the options shown (``requires`` may hide some)."""


class CommentRequired(Exception):
    pass


class ContentChanged(Exception):
    """The decision was made on a page showing something else than the run waits with."""


class UnknownVersion(Exception):
    pass


class ModelNotAllowed(Exception):
    """The run would use a model the data outbound policy does not allow (aep.policy)."""


class ProjectProblem(Exception):
    """The run cannot bind the project as asked (docs/api/projects.md); the message is
    for people."""


class NoDraftToRun(Exception):
    pass


class NotAFlowRun(Exception):
    """Only a flow app's run can be rerun from one of its steps."""


class CannotStartThere(Exception):
    """The run cannot start at that step; the message is for people."""


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
        "app_error",
    }
)
# How deep app nodes may call apps: A calls B calls C.
MAX_CALL_DEPTH = 3


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


class InvalidNotes(Exception):
    """Notes that cannot be taken; ``problems`` located in ``notes`` (``()`` is the list,
    ``(0, "item")`` the first note's item)."""

    def __init__(self, problems: list[tuple[tuple[int | str, ...], str]]) -> None:
        super().__init__("；".join(message for _, message in problems))
        self.problems = problems


MAX_NOTES = 50
MAX_NOTE_TEXT = 500


def _notes(pending: dict[str, Any], notes: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """The notes with the names of what they are on (``item``, ``label``, ``text``)."""
    if not notes:
        return []
    if pending.get("kind") == "form":
        raise InvalidNotes([((), "填写的步骤不能挂意见")])
    if len(notes) > MAX_NOTES:
        raise InvalidNotes([((), f"最多挂 {MAX_NOTES} 条意见")])
    data = (pending.get("details") or {}).get("data")
    problems: list[tuple[tuple[int | str, ...], str]] = []
    named = []
    for i, note in enumerate(notes):
        item, text = note["item"], note["text"]
        label = human.note_label(data, item)
        if label is None:
            problems.append(((i, "item"), f"页面上没有「{item[:60]}」这一项，挂不了意见"))
        if not text.strip() or len(text) > MAX_NOTE_TEXT:
            problems.append(((i, "text"), f"意见要写 1–{MAX_NOTE_TEXT} 个字，不能只有空格"))
        named.append({"item": item, "label": label or "", "text": text})
    if problems:
        raise InvalidNotes(problems)
    return named


@dataclass(frozen=True)
class RunView:
    record: RunRecord
    messages: list[dict[str, Any]]
    pending: dict[str, Any] | None
    result: dict[str, Any] | None
    trace_id: str | None = None
    # A flow app's numbered sources, each with ``cited`` (see flowgraph.cited).
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RunSettled:
    """A run that just started waiting for a person, finished or failed, with what a
    notice about it needs (aep.notify)."""

    record: RunRecord
    pending: dict[str, Any] | None
    app_title: str
    title: str | None


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
        # Told each time a run settles (waiting, done, error): the platform's notices.
        self.on_settled: Callable[[RunSettled], Awaitable[None]] | None = None
        # Where model calls are recorded (aep.models.CallStore), for each step's usage.
        self.calls: Any = None

    @property
    def llm_client(self) -> Any:
        """The model gateway client (``aep.llm``)."""
        return self._ctx.llm_client

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

    async def outcomes(self, since: datetime) -> dict[str, tuple[int, int]]:
        """Per app, runs since ``since`` that ended done and error (aep.runtime.store)."""
        return await self._store.outcomes(since)

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
            people=frozenset(
                n["id"] for n in flowgraph.nodes(config) if n["type"] in ("start", "human")
            ),
            call_app=self.call_app,
        )

    async def app_shape(self, app: str) -> flownodes.AppShape | None:
        """The app as an app node calling it sees it (its current version), or None when
        there is no such app."""
        kind = await self.kind(app)
        if kind is None:
            return None
        title = (await self.app_titles()).get(app, app)
        if kind != "flow":
            return flownodes.AppShape(app, kind, title)
        await self.ensure_versions(app)
        found = await self.versions.version(app, await self.versions.current(app))
        if found is None:
            return None
        config = self.flow_config(app, found.config)
        nodes = flowgraph.nodes(config)
        start = next((n for n in nodes if n["type"] == "start"), {})
        outputs: list[str] = []
        for n in nodes:
            if n["type"] == "end":
                outputs += [o["key"] for o in n.get("outputs") or [] if o["key"] not in outputs]
        return flownodes.AppShape(
            app, kind, title, list(start.get("inputs") or []), outputs, start.get("project")
        )

    async def _callers(self, record: RunRecord) -> list[str]:
        """The apps up the chain of app nodes that led to this run, this run's first."""
        chain = [record.app]
        current: RunRecord | None = record
        while current is not None and (current.trigger or {}).get("kind") == "app":
            current = await self._store.get(str(current.trigger["run_id"]))  # type: ignore[index]
            if current is not None:
                chain.append(current.app)
            if len(chain) > MAX_CALL_DEPTH + 1:
                break
        return chain

    async def call_app(
        self, app: str, input: dict[str, Any], state: dict[str, Any], node: str, timeout: int
    ) -> flownodes.AppCall:
        """Run ``app`` for node ``node`` of the run whose state this is and wait for it
        (docs/api/flow-apps.md, "app 调用应用"). A run of it the node started before a
        restart and that has not finished is waited for, not started again."""
        fail = flownodes.NodeFailure
        caller = await self._store.get(str(state.get("run_id")))
        if caller is None:
            raise fail(flownodes.INPUT_ERROR, "这里不能调用别的应用")
        shape = await self.app_shape(app)
        title = shape.title if shape else app
        if shape is None or shape.kind != "flow":
            raise fail(flownodes.INPUT_ERROR, f"没有可以调用的编排应用 {app}")
        chain = await self._callers(caller)
        if app in chain:
            raise fail(flownodes.INPUT_ERROR, f"应用「{title}」已经在调用链上，不能再调用它")
        if len(chain) >= MAX_CALL_DEPTH:
            raise fail(flownodes.INPUT_ERROR, f"应用最多嵌套调用 {MAX_CALL_DEPTH} 层")
        record = await self._store.called_by(caller.run_id, node)
        if record is None:
            bound = caller.project
            project = (
                {
                    "project_id": bound["project_id"],
                    "environment": bound["environment"]["key"],
                    "params": {k: v for k, v in bound["values"].items() if v is not None},
                }
                if bound and shape.project
                else None
            )
            titles = await self.app_titles()
            try:
                # Its own run, in a context of its own: the caller's step tracking and
                # trace (context variables) must not see the called run's steps.
                record = await asyncio.create_task(
                    self.create(
                        app,
                        input,
                        requester=caller.created_by,
                        project=project,
                        trigger={
                            "kind": "app",
                            "run_id": caller.run_id,
                            "node": node,
                            "title": titles.get(caller.app, caller.app),
                        },
                    ),
                    context=contextvars.Context(),
                )
            except InvalidValues as exc:
                said = "；".join(f"{k}：{v}" for k, v in exc.problems.items())
                raise fail(flownodes.INPUT_ERROR, f"调用「{title}」的输入不对：{said}") from exc
            except (UnknownApp, UnknownVersion, ProjectProblem, ModelNotAllowed) as exc:
                raise fail(flownodes.INPUT_ERROR, f"不能调用「{title}」：{exc}") from exc
        try:
            done = await self._wait_called(record.run_id, timeout)
        except asyncio.CancelledError:
            # The calling run was cancelled (or the process stops): so is the called one.
            await self._stop_called(record.run_id, "调用它的运行停了")
            raise
        if done.status == "waiting":
            await self._stop_called(record.run_id, "被调用的应用不能等人工处理")
            raise fail(
                flownodes.INPUT_ERROR,
                f"应用「{title}」要人工处理，不能被调用；人工确认请放在调用它的应用里",
            )
        if done.status == "running":
            await self._stop_called(record.run_id, f"超过 {timeout} 秒还没结束")
            raise fail("timeout", f"调用「{title}」超过 {timeout} 秒还没结束")
        if done.status != "done":
            why = done.error or ("被取消了" if done.status == "cancelled" else "失败了")
            raise fail("app_error", f"调用「{title}」没有完成：{why}")
        view = await self.get(record.run_id)
        result = view.result if isinstance(view.result, dict) else {}
        return flownodes.AppCall(record.run_id, title, result)

    async def _wait_called(self, run_id: str, timeout: int) -> RunRecord:
        """The called run once it is done, failed, cancelled or waits for a person, or as
        it is when ``timeout`` seconds are up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = await self._store.get(run_id)
            if found is None:
                raise flownodes.NodeFailure("app_error", "被调用的运行不见了")
            left = deadline - loop.time()
            if found.status != "running" or left <= 0:
                return found
            task = self._running.get(run_id)
            if task is not None:
                await asyncio.wait({task}, timeout=min(left, 5))
            else:
                # Run by another process (or not started yet): look again shortly.
                await asyncio.sleep(min(left, 0.5))

    async def _stop_called(self, run_id: str, reason: str) -> None:
        try:
            await self.cancel(run_id, None, reason)
        except (RunNotFound, NotCancellable):
            pass

    async def _models(
        self, app: str, config: dict[str, Any], input: dict[str, Any], default: str | None = None
    ) -> list[tuple[str, str]]:
        """Every model the run may call, with the title of the node that calls it: models,
        and the embedding and rerank models of the knowledge it searches. A node that names
        no model counts as ``default`` (the platform's default model unless given)."""
        used: list[tuple[str, str]] = []
        fallback = default or self.default_model

        async def bases(kb_ids: Any, title: str, mode: Any) -> None:
            # Keyword search embeds nothing; a setting left out is vector search.
            if mode == "keyword":
                return
            for kb_id in kb_ids if isinstance(kb_ids, list) else []:
                base = await self.knowledge.get(kb_id) if isinstance(kb_id, str) else None
                if base is not None:
                    used.append((base.embedding_model, title))

        if app in self._graphs:
            options = self.options[app]
            nodes = await appconfig.load(config, options, self.skills)
            chosen = input.get("models") if isinstance(input.get("models"), dict) else {}
            drawn = self._graphs[app].get_graph().nodes
            for name, node in nodes.items():
                found = drawn.get(name)
                design = (found.metadata or {}).get("design") if found else None
                title = design.get("title") or name if isinstance(design, dict) else name
                if options[name].model:
                    used.append((chosen.get(name) or node.model or fallback, title))
                await bases(
                    node.params.get("knowledge_bases"), title, node.params.get("knowledge_mode")
                )
            return used
        for node in flowgraph.nodes(config):
            title = node.get("title") or node["id"]
            if node["type"] in ("llm", "classifier", "agent"):
                used.append((node.get("model") or fallback, title))
            search = node if node["type"] == "knowledge" else None
            if node["type"] == "agent" and isinstance(node.get("knowledge"), dict):
                search = node["knowledge"]
            if search is not None:
                retrieval = search.get("retrieval")
                mode = retrieval.get("mode") if isinstance(retrieval, dict) else None
                await bases(search.get("knowledge_bases"), title, mode)
                rerank = retrieval.get("rerank") if isinstance(retrieval, dict) else None
                if isinstance(rerank, dict) and isinstance(rerank.get("model"), str):
                    used.append((rerank["model"], title))
        return used

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
        project: dict[str, Any] | None = None,
        seed: dict[str, Any] | None = None,
        start_at: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Start a run of the app's current version, or of ``app_version``; with ``config``
        (a draft's configuration, for its eval) or ``draft`` (the draft as it is now) the
        run belongs to no version. A flow app's input must fit its start node's inputs.
        ``requester`` is who asks for it, as they say; ``project`` the project it binds
        (``project_id``, ``environment``, ``params``; docs/api/projects.md); ``trigger``
        what started it when no person did (docs/api/schedules.md)."""
        app_version, config, bound, rules = await self._prepare(
            app, input, app_version, config, draft, project
        )
        record = await self._store.create(
            app,
            input,
            app_version,
            config,
            requester,
            rerun_of,
            rules.as_dict(),
            bound,
            start_at,
            trigger,
        )
        # A rerun from a step starts there, with the state the source run had then.
        payload = {**(seed or {}), "input": input}
        if start_at:
            payload["jump"] = start_at["node"]
        self._spawn(record, payload, "start", input)
        return record

    async def check_start(
        self, app: str, input: dict[str, Any], project: dict[str, Any] | None
    ) -> None:
        """Raise what :meth:`create` would for a run of the app's current version with this
        input and project, without starting one (a schedule is checked when saved)."""
        await self._prepare(app, input, None, None, False, project)

    async def status_of(self, run_id: str) -> str | None:
        found = await self._store.get(run_id)
        return found.status if found else None

    async def _prepare(
        self,
        app: str,
        input: dict[str, Any],
        app_version: int | None,
        config: dict[str, Any] | None,
        draft: bool,
        project: dict[str, Any] | None,
    ) -> tuple[int | None, dict[str, Any], dict[str, Any] | None, policy.DataPolicy]:
        """The version and configuration a new run uses, the project it binds and the
        outbound policy in force, all checked."""
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
        bound = await self._bind(app, kind, config, project)
        platform = policy.platform_default(self._ctx.settings)
        rules = projects.effective_policy(bound, platform)
        if rules.models is not None:
            # The project is named when its own policy limits the models.
            named = bound["name"] if bound and (bound["policy"] or {}).get("models") else None
            for model, where in await self._models(app, config, input):
                problem = policy.model_problem(rules, model, where, named)
                if problem:
                    raise ModelNotAllowed(problem)
        return app_version, config, bound, rules

    async def _bind(
        self, app: str, kind: str, config: dict[str, Any], asked: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The snapshot of the project the run binds, checked (docs/api/projects.md,
        "新建运行"); None when it binds none."""
        start = (
            next((n for n in flowgraph.nodes(config) if n["type"] == "start"), {})
            if kind == "flow"
            else {}
        )
        uses = start.get("project")
        title = (await self.app_titles()).get(app, app)
        if asked is None:
            if uses == "required":
                raise ProjectProblem(f"应用「{title}」需要选择一个项目")
            return None
        if uses is None:
            raise ProjectProblem(f"应用「{title}」不使用项目")
        found = await self._ctx.projects.get(str(asked.get("project_id") or ""))
        if found is None:
            raise ProjectProblem(f"没有项目 {asked.get('project_id')}")
        try:
            snapshot = projects.snapshot(found, asked.get("environment"), asked.get("params"))
        except projects.BindError as exc:
            raise ProjectProblem(str(exc)) from exc
        defined = {p["key"] for p in snapshot["params"]}
        for node in flowgraph.nodes(config):
            skip = NOT_SETTINGS | flownodes.TYPES[node["type"]].verbatim
            settings = {k: v for k, v in node.items() if k not in skip}
            for _, text in refs.strings(settings):
                for target, name in refs.find(text):
                    if target == refs.PARAM and name not in defined:
                        raise ProjectProblem(
                            f"应用用到了参数 {name}，项目「{found.name}」没有定义它"
                        )
        return snapshot

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
        await self._usage(run_id, flow["steps"])
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

    async def _usage(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        """Each step's model calls: the run's calls from its node within its time."""
        calls = await self.calls.for_run(run_id) if self.calls is not None else []
        now = datetime.now(UTC)
        for step in steps:
            start = datetime.fromisoformat(step["started_at"]) if step["started_at"] else None
            end = datetime.fromisoformat(step["finished_at"]) if step["finished_at"] else now
            mine = [c for at, c in calls if c.node == step["node"] and start and start <= at <= end]
            if not mine:
                step["usage"] = None
                continue
            costs = [c.cost_usd for c in mine if c.cost_usd is not None]
            step["usage"] = {
                "calls": len(mine),
                "prompt_tokens": sum(c.prompt_tokens for c in mine),
                "completion_tokens": sum(c.completion_tokens for c in mine),
                "cost_usd": round(sum(costs), 6) if costs else None,
            }

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
        notes: list[dict[str, str]] | None = None,
        seen: str | None = None,
    ) -> RunRecord:
        """Go on with a waiting run, checked in the order docs/api/flow-apps.md gives
        ("人工节点在工作台上的样子"). An approval's answer also has ``values`` (the answers
        to its questions), ``notes`` (with the name of what each is on), ``changed`` and
        ``submitted``: the option chosen when it turned into ``changed_branch``."""
        view = await self.get(run_id)
        pending = view.pending
        if view.record.status != "waiting" or pending is None:
            raise NotWaiting(run_id)
        shown = (pending.get("details") or {}).get("seen")
        if seen is not None and isinstance(shown, str) and seen != shown:
            raise ContentChanged(run_id)
        if decision not in pending.get("options", []):
            raise InvalidDecision(decision)
        answer: dict[str, Any] = {"decision": decision, "comment": comment, "actor": actor}
        if pending.get("kind") == "form":
            checked, problems = flownodes.answers(pending.get("fields") or [], values or {})
            if problems:
                raise InvalidValues(problems)
            _notes(pending, notes)
            answer["values"] = checked
        else:
            asked = pending.get("fields") or []
            checked, problems = flownodes.answers(asked, values or {}) if asked else ({}, {})
            if problems:
                raise InvalidValues(problems)
            named = _notes(pending, notes)
            differs = flownodes.changed(asked, checked)
            remark = bool((comment or "").strip())
            required = pending.get("comment_required", [])
            turn = pending.get("changed_branch")
            submitted = None
            if (
                turn in pending.get("options", [])
                and decision not in required
                and decision != turn
                and (differs or remark or named)
            ):
                submitted, decision = decision, turn
            if decision in required and not (remark or named or differs):
                raise CommentRequired(decision)
            answer |= {
                "decision": decision,
                "values": checked,
                "notes": named,
                "changed": differs,
                "submitted": submitted,
            }
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
        uses the current version. A run that bound a project binds its current settings,
        with the same environment and parameter values."""
        source = await self._store.get(run_id)
        if source is None:
            raise RunNotFound(run_id)
        draft = source.app_version is None and source.config is not None
        project = await self._same_project(source)
        return await self.create(
            source.app,
            source.input if input is None else input,
            source.app_version,
            config=source.config if draft else None,
            requester=requester,
            rerun_of=source.run_id,
            project=project,
        )

    async def _same_project(self, source: RunRecord) -> dict[str, Any] | None:
        """The project choice that binds the source run's project again."""
        if not source.project:
            return None
        project_id = source.project["project_id"]
        if await self._ctx.projects.get(project_id) is None:
            raise ProjectProblem(f"项目 {project_id} 已经删除，不能重新运行")
        return {
            "project_id": project_id,
            "environment": source.project["environment"]["key"],
            "params": {k: v for k, v in source.project["values"].items() if v is not None},
        }

    async def rerun_from(
        self, run_id: str, node: str, use_draft: bool, requester: str | None = None
    ) -> RunRecord:
        """A new run that starts at ``node`` with the state the source run had right
        before it last ran there, on the app's draft or on the source run's own
        configuration (docs/api/evals.md, "从某一步重跑")."""
        source = await self._store.get(run_id)
        if source is None:
            raise RunNotFound(run_id)
        if await self.kind(source.app) != "flow":
            raise NotAFlowRun(run_id)
        version, config = source.app_version, source.config
        if use_draft:
            found = await self.versions.draft(source.app)
            if found is None:
                raise NoDraftToRun(source.app)
            version, config = None, found.config
        chosen = self.flow_config(source.app, config)
        target = next((n for n in flowgraph.nodes(chosen) if n["id"] == node), None)
        if target is None:
            where = "草稿" if use_draft else "原运行的配置"
            raise CannotStartThere(f"{where}里没有节点 {node}")
        if target["type"] == "start":
            raise CannotStartThere("从开始节点重跑就是重新运行，请用“复制并重新发起”")
        before = None
        graph = await self._graph(source)
        async for snapshot in graph.aget_state_history(_config(run_id)):
            if node in snapshot.next:
                before = snapshot
                break
        if before is None:
            raise CannotStartThere(f"原运行没有跑到「{target.get('title') or node}」这一步")
        seed = {
            k: v for k, v in (before.values or {}).items() if k not in ("input", "jump", "result")
        }
        return await self.create(
            source.app,
            source.input,
            version,
            config=None if version is not None else config,
            requester=requester,
            rerun_of=source.run_id,
            project=await self._same_project(source),
            seed=seed,
            start_at={"node": node, "title": target.get("title") or node, "run_id": run_id},
        )

    async def answers_given(self, run_id: str) -> dict[str, dict[str, Any]]:
        """How people answered the run's human nodes, the last answer of each: an
        approval's ``decision`` and ``comment`` (and ``values`` when it asked any), a
        form's ``values``. Only flow runs have any."""
        record = await self._store.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        if await self.kind(record.app) != "flow":
            return {}
        config = self.flow_config(record.app, record.config)
        state = await (await self._graph(record)).aget_state(_config(run_id))
        found = (state.values or {}).get("vars") or {}
        out: dict[str, dict[str, Any]] = {}
        for item in flowgraph.nodes(config):
            given = found.get(item["id"])
            if item["type"] != "human" or not isinstance(given, dict):
                continue
            if item.get("mode", "approval") == "form":
                keys = [f["key"] for f in item.get("fields") or []]
                out[item["id"]] = {"values": {k: given.get(k) for k in keys}}
            else:
                answer: dict[str, Any] = {
                    "decision": given.get("decision"),
                    "comment": given.get("comment"),
                }
                if given.get("values"):
                    answer["values"] = given["values"]
                out[item["id"]] = answer
        return out

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
        if await self._store.set_status(run_id, status, expected=("running",), **kwargs):
            self._announce(run_id)

    def _announce(self, run_id: str) -> None:
        """Tell ``on_settled`` in the background: a slow mail server or webhook never
        holds up the run, and a failure there is only logged."""
        listener = self.on_settled
        if listener is None:
            return

        async def announce() -> None:
            try:
                view = await self.get(run_id)
                titles = await self.app_titles()
                record = view.record
                await listener(
                    RunSettled(
                        record, view.pending, titles.get(record.app, record.app), self.title(record)
                    )
                )
            except Exception:
                log.exception("could not announce run %s", run_id)

        task = asyncio.create_task(announce())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
                        # What of the data may go to which models (aep.policy), and
                        # the snapshot of the project the run binds.
                        "policy": record.policy,
                        "project": record.project,
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
                outcome = None
                if status == "done" and record.app not in self._graphs:
                    flow = self.flow_config(record.app, record.config)
                    outcome = flowgraph.outcome(flow, state.values or {})
                await self._finish(record.run_id, status, outcome=outcome)
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
