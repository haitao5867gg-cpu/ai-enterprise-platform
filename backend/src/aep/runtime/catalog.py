"""The apps people can configure: their design, versions, draft and the eval gate.

``AppCatalog`` serves the Apps API (``aep.api.apps``). Changing an app means
saving a draft of its configuration (checked against the app's options, see
``aep.runtime.appconfig``), then publishing it as the next version. An app with an
eval set (its module's ``EVAL_SET``, under AEP_EVALS_DIR) publishes a draft only
after the draft passed it: ``start_eval`` runs every case of the set in the
background, as real runs of the draft's configuration that never go to prod, scores
them like ``aep-eval`` (``aep.evals``) and keeps the result on the draft. The draft
passes when the average case score reaches AEP_EVAL_PUBLISH_THRESHOLD.

Flow apps (``aep.runtime.flow``) are also created, renamed and archived here; their
configuration is their whole structure, checked by ``aep.runtime.flow.check``.
"""

import asyncio
import copy
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from aep import conversations as chat
from aep import eval_sets, evals, llm, policy
from aep.config import Settings
from aep.connector_urls import same_url
from aep.datasources import DATABASE_KINDS, DataSourceStore
from aep.knowledge.rerank import rerank_options
from aep.runtime import agent_app, appconfig, design, manual
from aep.runtime.appconfig import ConfigError
from aep.runtime.flow import builtin as flowbuiltin
from aep.runtime.flow import files as flowfiles
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow import refs
from aep.runtime.flow.check import check_common, check_config
from aep.runtime.flow.nodes import TYPES, CheckEnv, NodeFailure, Problems, why_not_callable
from aep.runtime.flow.store import AppExists
from aep.runtime.service import (
    CommentRequired,
    InvalidDecision,
    InvalidValues,
    RunNotFound,
    RunService,
    RunView,
    TurnCleared,
)
from aep.runtime.store import FINISHED
from aep.runtime.versions import AppVersion, Draft, NoDraft, VersionNotFound
from aep.skills import Skill

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
MODELS_TTL_SECONDS = 60.0


class AppNotFound(LookupError):
    pass


class EvalUnavailable(Exception):
    """The draft cannot be evaluated now; the message says why."""


class NotPublishable(Exception):
    """The draft cannot be published now; the message says why."""


class NotAFlowApp(Exception):
    """Only flow apps can be changed this way; the message says why."""


class BadSource(Exception):
    """A new app's source (template, app or version) does not exist."""


class NodeNotFound(LookupError):
    pass


class NotTestable(Exception):
    """This node cannot be run on its own; the message says why."""


class EvalSetReadOnly(Exception):
    """The app's eval set is a file in the repository."""


class CaseFromRunProblem(Exception):
    """The run cannot be saved as a case; the message says why. ``status`` is the HTTP
    status that fits."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


# How long a node tried on its own may take.
NODE_TEST_SECONDS = 120
MASK = "******"


NOT_CHAT = "这个应用没有开对话，用例不能带历史"


def _check_out(check: evals.Check) -> dict[str, Any]:
    out = {"kind": check.layer, "name": check.name, "passed": check.passed, "detail": check.detail}
    return out | check.extra


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Stands for "the platform's default model" while working out which models apps use.
DEFAULT_MARK = "\x00default"


class GatewayModels:
    """The model aliases the LiteLLM gateway offers, asked at most once a minute."""

    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client
        self._cached: tuple[float, list[str]] | None = None

    async def __call__(self) -> list[str] | None:
        """The aliases, or None when the gateway does not answer."""
        if self._cached and time.monotonic() - self._cached[0] < MODELS_TTL_SECONDS:
            return self._cached[1]
        try:
            listed = await self._client.models.list()
            names = sorted({m.id async for m in listed})
        except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
            log.warning("could not list the gateway's models: %s", exc)
            return None
        self._cached = (time.monotonic(), names)
        return names


class AppCatalog:
    def __init__(
        self,
        runs: RunService,
        datasources: DataSourceStore,
        settings: Settings,
        models: Any = None,
        eval_sets_store: eval_sets.EvalSetStore | None = None,
    ) -> None:
        self._runs = runs
        self.eval_sets = eval_sets_store or eval_sets.MemoryEvalSetStore()
        self._datasources = datasources
        self._settings = settings
        self._models = models
        # The model page's dropdown entries (aep.models.ModelService.choices), when wired.
        self.model_choices: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None
        self._evals: dict[str, asyncio.Task] = {}
        self._starting: dict[str, asyncio.Lock] = {}

    # Reading

    async def _check(self, app: str) -> str:
        """The app's kind (``code`` or ``flow``)."""
        kind = await self._runs.kind(app)
        if kind is None:
            raise AppNotFound(app)
        return kind

    def eval_set(self, app: str) -> Path | None:
        """Where the app's eval set is, if the app has one. The file may be missing (a
        wrong AEP_EVALS_DIR); the gate still applies, and evals fail until it is there.
        Flow apps have none yet."""
        if app not in self._runs.code_apps:
            return None
        build = self._runs.builder(app)
        name = getattr(sys.modules.get(build.__module__), "EVAL_SET", None)
        return Path(self._settings.evals_dir) / name if name else None

    async def has_eval_set(self, app: str) -> bool:
        """Whether publishing waits for a passed eval: a file eval set, or a platform one
        with at least one case."""
        if self.eval_set(app) is not None:
            return True
        found = await self.eval_sets.get(app)
        return bool(found and found.cases)

    async def models(self) -> list[str] | None:
        return await self._models() if self._models else None

    async def rerank_models(self) -> list[dict[str, str]]:
        models = await self.models()
        return rerank_options(
            self._settings, models if models is not None else [self._runs.default_model]
        )

    async def summaries(self) -> list[dict[str, Any]]:
        return [await self._summary(app) for app in await self._runs.app_names()]

    async def _summary(self, app: str) -> dict[str, Any]:
        await self._runs.ensure_versions(app)
        if app in self._runs.code_apps:
            d = self._runs.app_design(app)
            title, summary, created_at = (d.title if d else app), (d.summary if d else ""), None
            kind = "code"
        else:
            flow = await self._runs.flows.get(app)
            assert flow is not None
            title, summary, created_at = flow.title, flow.summary, flow.created_at.isoformat()
            kind = flow.kind
        return {
            "app": app,
            "kind": kind,
            "title": title,
            "summary": summary,
            "created_at": created_at,
            "current_version": await self._runs.versions.current(app),
            "has_draft": await self._runs.versions.draft(app) is not None,
            "has_eval_set": await self.has_eval_set(app),
        }

    @property
    def runs(self) -> RunService:
        return self._runs

    def config_of(self, app: str, config: dict[str, Any]) -> dict[str, Any]:
        """A stored configuration as the app reads it."""
        return self._config(app, config)

    def _config(self, app: str, config: dict[str, Any]) -> dict[str, Any]:
        return config if app in self._runs.code_apps else self._runs.flow_config(app, config)

    def _version_out(self, v: AppVersion, current: int) -> dict[str, Any]:
        # An agent app's configuration shows as it was set up, not as its generated graph.
        shown = v.config if agent_app.is_agent(v.config) else self._config(v.app, v.config)
        return {
            "version": v.version,
            "status": "current" if v.version == current else "previous",
            "config": shown,
            "note": v.note,
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat(),
            "eval": v.eval,
        }

    def _draft_out(self, d: Draft) -> dict[str, Any]:
        return {
            "version": None,
            "status": "draft",
            "config": d.config,
            "note": None,
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat(),
            "eval": d.eval,
        }

    async def versions(self, app: str) -> list[dict[str, Any]]:
        await self._check(app)
        await self._runs.ensure_versions(app)
        current = await self._runs.versions.current(app)
        return [self._version_out(v, current) for v in await self._runs.versions.versions(app)]

    async def detail(self, app: str) -> dict[str, Any]:
        kind = await self._check(app)
        summary = await self._summary(app)
        store = self._runs.versions
        current = await store.version(app, summary["current_version"])
        assert current is not None
        draft = await store.draft(app)
        chat: dict[str, Any] | None = None
        if kind == "code":
            options = self._runs.options[app]
            configs = await appconfig.load(current.config, options, self._runs.skills)
            nodes, edges = design.shape(self._runs.graph(app), configs)
            for e in edges:
                e["label"] = None
            d = self._runs.app_design(app)
            texts = {
                "pattern": d.pattern if d else "",
                "knowledge": d.knowledge if d else "",
                "skills_text": d.skills if d else "",
            }
            inputs = None
            # Code apps bind no project (docs/api/projects.md).
            uses_project = None
        else:
            config = self._runs.flow_config(app, current.config)
            options = {}
            nodes, edges = flowgraph.shape(config, await self._runs.runtime(config))
            if agent_app.is_agent(current.config):
                # What the use page shows before the first message (docs/api/agent-apps.md).
                chat = current.config.get("chat") or {"opening": "", "suggestions": []}
            texts = {
                "pattern": flowgraph.DESIGN["pattern"],
                "knowledge": flowgraph.DESIGN["knowledge"],
                "skills_text": flowgraph.DESIGN["skills"],
            }
            inputs = flowgraph.inputs(config)
            start = next((n for n in flowgraph.nodes(config) if n["type"] == "start"), {})
            uses_project = start.get("project")
        skills: list[Skill] = await self._runs.skills.all()
        models = await self.models()
        default = self._runs.default_model
        return (
            summary
            | texts
            | {
                "chat": chat,
                "inputs": inputs,
                "project": uses_project,
                "nodes": [
                    {
                        "id": n["id"],
                        "title": n["title"],
                        "kind": n["kind"],
                        "type": n.get("type"),
                        "design": n["design"],
                        "settings": options[n["id"]].settings() if n["id"] in options else [],
                    }
                    for n in nodes
                ],
                "edges": edges,
                "secrets": await self._secrets(app, current, draft),
                "current": self._version_out(current, current.version),
                "draft": self._draft_out(draft) if draft else None,
                "options": {
                    "models": models if models is not None else [default],
                    "default_model": default,
                    "model_choices": await self.model_choices() if self.model_choices else [],
                    "rerank_models": rerank_options(
                        self._settings, models if models is not None else [default]
                    ),
                    "skills": [
                        {
                            "name": s.name,
                            "description": s.description,
                            "published_version": s.published_version,
                        }
                        for s in skills
                        if s.published_version is not None
                    ],
                    "datasources": [
                        {"datasource_id": d.datasource_id, "name": d.name, "kind": d.kind}
                        for d in await self._datasources.all()
                    ],
                    "knowledge_bases": [
                        {"kb_id": kb.kb_id, "name": kb.name, "document_count": kb.document_count}
                        for kb in await self._runs.knowledge.all()
                    ],
                    **({"apps": await self._callable_apps(app)} if chat is not None else {}),
                },
            }
        )

    async def _callable_apps(self, app: str) -> list[dict[str, Any]]:
        """The apps an agent app may call, and why the others may not
        (docs/api/agent-apps.md, "应用接口的变化")."""
        out: list[dict[str, Any]] = []
        for name in await self._runs.app_names():
            if name == app:
                continue
            shape = await self._runs.app_shape(name)
            reason = why_not_callable(shape, name)
            kind = await self._runs.app_kind(name)
            if kind is None:
                continue
            out.append(
                {
                    "app": name,
                    "title": shape.title if shape else name,
                    "kind": kind,
                    "callable": reason is None,
                    "reason": reason,
                }
            )
        return out

    async def manual(self, app: str, version: int | None = None, draft: bool = False) -> dict:
        """The manual of the app's ``version`` (the current one if None), or of its draft
        (``NoDraft`` without one); see ``aep.runtime.manual``."""
        await self._check(app)
        await self._runs.ensure_versions(app)
        if draft:
            found_draft = await self._runs.versions.draft(app)
            if found_draft is None:
                raise NoDraft(app)
            return await self._manual(app, found_draft.config, None)
        number = version or await self._runs.versions.current(app)
        found = await self._runs.versions.version(app, number)
        if found is None:
            raise VersionNotFound(f"{app} v{number}")
        return await self._manual(app, found.config, number)

    async def run_manual(self, run_id: str) -> dict[str, Any]:
        """The manual of the configuration a run used (``RunNotFound`` without the run)."""
        record = (await self._runs.get(run_id)).record
        return await self._manual(record.app, record.config, record.app_version)

    async def _manual(
        self, app: str, config: dict[str, Any] | None, version: int | None
    ) -> dict[str, Any]:
        summary = await self._summary(app)
        head = {
            "app": app,
            "title": summary["title"],
            "summary": summary["summary"],
            "kind": summary["kind"],
            "version": version,
        }
        lookups = manual.Lookups(
            self._runs.skills,
            self._runs.knowledge,
            self._datasources,
            self._settings,
            self._runs.default_model,
        )
        if app in self._runs.code_apps:
            options = self._runs.options[app]
            nodes, _ = design.shape(self._runs.graph(app), {})
            stored = config or appconfig.default_config(options)
            return await manual.code_manual(head, nodes, stored, options, lookups)
        return await manual.flow_manual(head, self._runs.flow_config(app, config), lookups)

    async def used_by(self) -> dict[str, list[dict[str, Any]]]:
        """Per skill, the app nodes whose current version uses it."""
        found: dict[str, list[dict[str, Any]]] = {}
        for app in await self._runs.app_names():
            await self._runs.ensure_versions(app)
            number = await self._runs.versions.current(app)
            current = await self._runs.versions.version(app, number)
            config = self._config(app, current.config) if current else {}
            if app in self._runs.code_apps:
                settings = list((config.get("nodes") or {}).items())
            else:
                settings = [(n["id"], n) for n in flowgraph.nodes(config)]
            for node, value in settings:
                for ref in value.get("skills") or []:
                    use = {"app": app, "node": node, "version": number}
                    use["skill_version"] = ref["version"]
                    found.setdefault(ref["name"], []).append(use)
        return found

    async def datasource_usage(self) -> dict[str, list[dict[str, Any]]]:
        """Per data source id (``canon_id``), the flow apps whose current version names it,
        as ``{"app", "title", "version", "via"}`` sorted by app. Like ``used_by``: archived
        apps, drafts and older versions do not count, nor code apps (their source is
        chosen per run). An app whose configuration cannot be read is left out."""
        await self._runs.ensure_flows()
        found: dict[str, list[dict[str, Any]]] = {}
        for flow in await self._runs.flows.all():
            try:
                number = await self._runs.versions.current(flow.app)
                current = await self._runs.versions.version(flow.app, number)
                if current is None:
                    continue
                uses = flowfiles.datasource_uses(self._runs.flow_config(flow.app, current.config))
            except Exception:  # noqa: BLE001 - one broken app must not hide the others
                log.warning("could not read the data sources of app %s", flow.app, exc_info=True)
                continue
            for datasource_id, via in uses.items():
                use = {"app": flow.app, "title": flow.title, "version": number, "via": via}
                found.setdefault(datasource_id, []).append(use)
        for uses_of_one in found.values():
            uses_of_one.sort(key=lambda use: use["app"])
        return found

    async def app_callers(self, app: str) -> list[dict[str, Any]]:
        """The apps whose current version calls ``app``, as ``{"app", "title", "version",
        "via"}`` sorted by app (docs/api/app-access.md, "入口"): ``via`` is ``node`` for an app
        node and ``agent`` for an agent that names it in its enabled ``apps``. Drafts, older
        versions and archived apps do not count; an app whose configuration cannot be read
        is left out."""
        await self._runs.ensure_flows()
        found: list[dict[str, Any]] = []
        for flow in await self._runs.flows.all():
            if flow.app == app:
                continue
            try:
                number = await self._runs.versions.current(flow.app)
                current = await self._runs.versions.version(flow.app, number)
                if current is None:
                    continue
                nodes = flowgraph.nodes(self._runs.flow_config(flow.app, current.config))
            except Exception:  # noqa: BLE001 - one broken app must not hide the others
                log.warning("could not read the calls of app %s", flow.app, exc_info=True)
                continue
            for via in ("node", "agent"):
                if any(
                    (n["type"] == "app" and n.get("app") == app)
                    if via == "node"
                    else n["type"] == "agent"
                    and any(
                        a.get("app") == app and a.get("enabled", True) for a in n.get("apps") or []
                    )
                    for n in nodes
                ):
                    found.append(
                        {"app": flow.app, "title": flow.title, "version": number, "via": via}
                    )
        return sorted(found, key=lambda use: (use["app"], use["via"]))

    async def model_usage(self) -> dict[str, list[dict[str, Any]]]:
        """Per model alias, the apps whose current version calls it, as ``{"app", "title",
        "version", "nodes", "via"}`` sorted by app (docs/api/models.md, "使用情况"): ``via``
        is ``default`` when every node that calls it names no model. Like
        ``datasource_usage``, drafts, older versions and archived apps do not count; an app
        whose configuration cannot be read is left out."""
        default = self._runs.default_model
        titles = await self._runs.app_titles()
        found: dict[str, list[dict[str, Any]]] = {}
        for app in await self._runs.app_names():
            try:
                await self._runs.ensure_versions(app)
                number = await self._runs.versions.current(app)
                current = await self._runs.versions.version(app, number)
                if current is None:
                    continue
                used = await self._runs._models(
                    app, self._config(app, current.config), {}, default=DEFAULT_MARK
                )
            except Exception:  # noqa: BLE001 - one broken app must not hide the others
                log.warning("could not read the models of app %s", app, exc_info=True)
                continue
            per_alias: dict[str, dict[str, Any]] = {}
            for model, title in used:
                alias = default if model == DEFAULT_MARK else model
                use = per_alias.setdefault(
                    alias,
                    {
                        "app": app,
                        "title": titles.get(app, app),
                        "version": number,
                        "nodes": [],
                        "via": "default",
                    },
                )
                if title not in use["nodes"]:
                    use["nodes"].append(title)
                if model != DEFAULT_MARK:
                    use["via"] = "literal"
            for alias, use in per_alias.items():
                found.setdefault(alias, []).append(use)
        for uses_of_one in found.values():
            uses_of_one.sort(key=lambda use: use["app"])
        return found

    async def knowledge_users(self, kb_id: str) -> list[str]:
        """Apps whose current version uses this base; drafts and older versions do not count."""
        await self._runs.ensure_flows()
        apps = sorted(
            {*self._runs.code_apps, *(a.app for a in await self._runs.flows.all(archived=True))}
        )
        found = []
        for app in apps:
            await self._runs.ensure_versions(app)
            number = await self._runs.versions.current(app)
            current = await self._runs.versions.version(app, number)
            if current is None:
                continue
            config = self._config(app, current.config)
            if app == "etl":
                plan = (config.get("nodes") or {}).get("plan") or {}
                bases = (plan.get("params") or {}).get("knowledge_bases", [])
            elif app in self._runs.code_apps:
                continue
            else:
                bases = flowfiles.knowledge_ids(config)
            if kb_id in bases:
                found.append(app)
        return found

    # Changing

    async def save_draft(self, app: str, raw: Any, actor: str | None) -> None:
        kind = await self._check(app)
        await self._runs.ensure_versions(app)
        if await self._runs.app_kind(app) == agent_app.KIND:
            config = await self.check_agent(raw, app=app)
        elif kind == "flow":
            config = await self.check_flow(raw, app=app)
        else:
            config = await appconfig.validate_config(
                raw,
                self._runs.options[app],
                self._runs.skills,
                await self.models(),
                self._runs.knowledge,
            )
        await self._runs.versions.save_draft(app, config, actor)

    def _env(
        self, models: list[str] | None, strict: bool = True, app: str | None = None
    ) -> CheckEnv:
        return CheckEnv(
            models,
            self._runs.skills,
            self._datasources,
            self._runs.tools,
            strict,
            self._runs.knowledge,
            rerank_options(
                self._settings, models if models is not None else [self._runs.default_model]
            ),
            apps=self._runs.app_shape,
            app=app,
        )

    async def check_flow(
        self, raw: Any, strict: bool = True, app: str | None = None
    ) -> dict[str, Any]:
        return await check_config(raw, self._env(await self.models(), strict, app))

    async def check_agent(
        self, raw: Any, strict: bool = True, app: str | None = None
    ) -> dict[str, Any]:
        """An agent app's configuration, checked as its generated graph is."""
        return await agent_app.check(raw, lambda flow: self.check_flow(flow, strict, app))

    async def discard_draft(self, app: str) -> None:
        await self._check(app)
        await self._runs.versions.discard_draft(app)

    # Flow apps

    async def create(
        self,
        app: str,
        title: str,
        summary: str,
        source: dict[str, Any],
        actor: str | None,
    ) -> None:
        """A new flow or agent app; its version 1 is a blank app, a template or a copy of
        an app's version (docs/api/agent-apps.md, "新建、模板、导出导入")."""
        if app in self._runs.code_apps or await self._runs.flows.get(app) is not None:
            raise AppExists(app)
        needs_setup = False
        kind = "flow"
        copied: eval_sets.EvalSet | None = None
        if source.get("template"):
            template = flowbuiltin.TEMPLATES.get(source["template"])
            if template is None:
                raise BadSource(f"没有模板 {source['template']}")
            raw, note = template.config, f"从模板「{template.title}」新建"
            kind = template.kind
            # An agent's data source is chosen too before it can run.
            needs_setup = "knowledge_base" in template.needs or (
                kind == agent_app.KIND and "datasource" in template.needs
            )
        elif source.get("app"):
            original = source["app"]
            kind = await self._runs.app_kind(original) or ""
            if kind not in ("flow", agent_app.KIND):
                raise BadSource(f"没有编排应用 {original}，代码应用不能复制")
            await self._runs.ensure_versions(original)
            number = source.get("version") or await self._runs.versions.current(original)
            found = await self._runs.versions.version(original, number)
            if found is None:
                raise BadSource(f"应用 {original} 没有版本 {number}")
            raw = (
                found.config
                if kind == agent_app.KIND
                else self._runs.flow_config(original, found.config)
            )
            note = f"复制自 {original} 版本 {number}"
            copied = await self.eval_sets.get(original)
        elif source.get("blank_agent"):
            raw, note, kind = agent_app.blank(), "新建空白智能体", agent_app.KIND
        else:
            raw, note = flowbuiltin.blank(), "新建空白应用"
        # Built-in templates that need a base intentionally have an empty selection.
        if needs_setup:
            config = copy.deepcopy(raw)
        elif kind == agent_app.KIND:
            config = await self.check_agent(raw)
        else:
            config = await self.check_flow(raw)
        await self._create_flow(app, title, summary, config, note, actor, needs_setup, kind)
        if source.get("app") and copied is not None:
            await self._copy_eval_set(copied, app, actor)

    async def _copy_eval_set(self, copied: eval_sets.EvalSet, app: str, actor: str | None) -> None:
        """A copy keeps the original's eval set, and so its publishing gate."""
        for case in copied.cases:
            case["from_run"] = None
        await self.eval_sets.put(
            eval_sets.EvalSet(
                app, copied.threshold, copied.judge_model, copied.cases, actor, datetime.now(UTC)
            )
        )

    async def convert_to_flow(
        self,
        app: str,
        new: str,
        title: str | None,
        summary: str | None,
        version: int | None,
        actor: str | None,
    ) -> None:
        """A new flow app whose version 1 is the graph a version of the agent app runs as,
        with its eval set; the agent app stays as it is (docs/api/agent-apps.md,
        "转成编排应用")."""
        await self._check(app)
        if await self._runs.app_kind(app) != agent_app.KIND:
            raise NotAFlowApp("只有智能体应用能转成编排应用")
        if new in self._runs.code_apps or await self._runs.flows.get(new) is not None:
            raise AppExists(new)
        await self._runs.ensure_versions(app)
        number = version or await self._runs.versions.current(app)
        found = await self._runs.versions.version(app, number)
        if found is None:
            raise VersionNotFound(f"{app} v{number}")
        original = await self._runs.flows.get(app)
        assert original is not None
        config = await self.check_flow(self._runs.flow_config(app, found.config))
        await self._create_flow(
            new,
            (title or original.title)[:50],
            (original.summary if summary is None else summary)[:500],
            config,
            f"由智能体应用 {app} 版本 {number} 转成",
            actor,
            False,
        )
        copied = await self.eval_sets.get(app)
        if copied is not None:
            await self._copy_eval_set(copied, new, actor)

    async def _create_flow(
        self,
        app: str,
        title: str,
        summary: str,
        config: dict[str, Any],
        note: str,
        actor: str | None,
        needs_setup: bool,
        kind: str = "flow",
    ) -> None:
        """Create version 1 and, when resources need choosing, keep the config as a draft."""
        initial = config
        if needs_setup and kind == agent_app.KIND:
            initial = await self.check_agent(agent_app.blank())
        elif needs_setup:
            initial = await self.check_flow(flowbuiltin.blank())
        await self._runs.flows.create(app, title, summary, actor, kind)
        await self._runs.versions.ensure(
            app, initial, f"新建空白应用（{note}的配置在草稿里）" if needs_setup else note, actor
        )
        if needs_setup:
            await self._runs.versions.save_draft(app, config, actor)

    async def _flow(self, app: str) -> None:
        if await self._check(app) != "flow":
            raise NotAFlowApp("代码应用的名称和流程由代码定义，不能在这里修改或删除")

    async def update(self, app: str, title: str | None, summary: str | None) -> None:
        await self._flow(app)
        await self._runs.flows.update(app, title, summary)

    async def archive(self, app: str) -> None:
        await self._flow(app)
        await self._runs.flows.archive(app)

    # Secrets

    async def _secrets(
        self, app: str, current: AppVersion, draft: Draft | None
    ) -> list[dict[str, Any]]:
        """The secrets the current version or the draft declares, and those that are set."""
        stored = {s.name: s for s in await self._runs.secrets.names(app)}
        declared: list[str] = []
        for config in [current.config, draft.config if draft else {}]:
            for name in (config.get("env") or {}).get("secrets") or []:
                if name not in declared:
                    declared.append(name)
        names = declared + sorted(set(stored) - set(declared))
        return [
            {
                "name": name,
                "set": name in stored,
                "updated_at": stored[name].updated_at.isoformat() if name in stored else None,
            }
            for name in names
        ]

    async def set_secret(self, app: str, name: str, value: str) -> None:
        await self._flow(app)
        await self._runs.secrets.set(app, name, value)

    async def delete_secret(self, app: str, name: str) -> None:
        await self._flow(app)
        await self._runs.secrets.delete(app, name)

    # Files

    async def export(self, app: str, version: int) -> tuple[str, str]:
        """The version as a YAML file: its name and its text."""
        await self._flow(app)
        found = await self._runs.versions.version(app, version)
        if found is None:
            raise VersionNotFound(f"{app} v{version}")
        flow = await self._runs.flows.get(app)
        assert flow is not None
        sources = {
            d.datasource_id: {"datasource_id": d.datasource_id, "name": d.name, "kind": d.kind}
            for d in await self._datasources.all()
        }
        # An agent app's file holds its own configuration, not the graph it runs as.
        config = (
            found.config
            if flow.kind == agent_app.KIND
            else self._runs.flow_config(app, found.config)
        )
        bases = {
            kb.kb_id: {"kb_id": kb.kb_id, "name": kb.name}
            for kb in await self._runs.knowledge.all()
        }
        text = flowfiles.dump(
            flow.title, flow.summary, app, version, config, sources, bases, flow.kind
        )
        return f"{app}-v{version}.yaml", text

    async def _file_config(self, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """A file's configuration as checked, and the graph it runs as."""
        if data["kind"] == agent_app.KIND:
            config = await self.check_agent(data["config"], strict=False)
            return config, agent_app.as_flow(config)
        config = await self.check_flow(data["config"], strict=False)
        return config, config

    async def _missing(
        self, app: str, config: dict[str, Any], requires: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """What a configuration names that this platform does not have."""
        out: list[dict[str, Any]] = []
        for name, version in flowgraph.skill_refs(config):
            if await self._runs.skills.version(name, version) is None:
                out.append(
                    {
                        "kind": "skill",
                        "name": name,
                        "version": version,
                        "message": "没有这个 Skill 版本",
                    }
                )
        named = {
            d.get("datasource_id"): d.get("name")
            for d in requires.get("datasources") or []
            if isinstance(d, dict)
        }
        for source in flowfiles.datasource_ids(config):
            if await self._datasources.get(source) is None:
                out.append(
                    {
                        "kind": "datasource",
                        "datasource_id": source,
                        "name": named.get(source),
                        "message": "没有这个数据源，请在节点里重新选择",
                    }
                )
        named_bases = {
            kb.get("kb_id"): kb.get("name")
            for kb in requires.get("knowledge_bases") or []
            if isinstance(kb, dict)
        }
        for kb_id in flowfiles.knowledge_ids(config):
            if await self._runs.knowledge.get(kb_id) is None:
                out.append(
                    {
                        "kind": "knowledge_base",
                        "kb_id": kb_id,
                        "name": named_bases.get(kb_id),
                        "message": "没有这个知识库，请在节点里重新选择",
                    }
                )
        models = await self.models()
        if models is not None:
            for model in dict.fromkeys(
                n["model"] for n in flowgraph.nodes(config) if n.get("model")
            ):
                if model not in models:
                    out.append(
                        {"kind": "model", "name": model, "message": "模型网关上没有这个别名"}
                    )
        for node in flowgraph.nodes(config):
            for entry in node.get("apps") or [] if node["type"] == "agent" else []:
                if await self._runs.app_shape(entry["app"]) is None:
                    out.append(
                        {
                            "kind": "app",
                            "name": entry["app"],
                            "message": "没有这个已发布的应用，请在智能体里重新选择",
                        }
                    )
        for name in (config.get("env") or {}).get("secrets") or []:
            if await self._runs.secrets.get(app, name) is None:
                out.append(
                    {"kind": "secret", "name": name, "message": "密钥没有值，请在应用设置里填写"}
                )
        return out

    async def import_app(
        self, app: str, title: str | None, summary: str | None, text: str, actor: str | None
    ) -> list[dict[str, Any]]:
        """A new flow app from a file. When the file names things this platform lacks
        (secrets aside), version 1 is a blank app and the file's configuration waits in
        the draft until it is fixed."""
        data = flowfiles.load(text)
        if app in self._runs.code_apps or await self._runs.flows.get(app) is not None:
            raise AppExists(app)
        config, shape = await self._file_config(data)
        problems = await self._missing(app, shape, data["requires"])
        name = (title or data["title"] or app)[:50]
        needs_setup = any(p["kind"] != "secret" for p in problems)
        await self._create_flow(
            app,
            name,
            (summary or data["summary"])[:500],
            config,
            "导入" if needs_setup else "从文件导入",
            actor,
            needs_setup,
            data["kind"],
        )
        return problems

    async def import_draft(self, app: str, text: str, actor: str | None) -> list[dict[str, Any]]:
        """A file imported as the draft of an existing flow app."""
        await self._flow(app)
        data = flowfiles.load(text)
        kind = await self._runs.app_kind(app)
        if data["kind"] != kind:
            raise flowfiles.BadFile(
                "这是智能体应用的文件，不能导入成编排应用的草稿"
                if data["kind"] == agent_app.KIND
                else "这是编排应用的文件，不能导入成智能体应用的草稿"
            )
        config, shape = await self._file_config(data)
        problems = await self._missing(app, shape, data["requires"])
        await self._runs.versions.save_draft(app, config, actor)
        return problems

    # Trying one node

    async def test_node(
        self, app: str, node_id: str, raw: dict[str, Any] | None, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one node of ``raw`` (else the draft, else the current version) with the
        variables it reads set to ``values`` (keyed ``node.field``). Nothing is kept."""
        await self._flow(app)
        if raw is None:
            draft = await self._runs.versions.draft(app)
            if draft is not None:
                raw = draft.config
            else:
                number = await self._runs.versions.current(app)
                found = await self._runs.versions.version(app, number)
                raw = found.config if found else {}
        config = self._runs.flow_config(app, raw)
        nodes = (config.get("graph") or {}).get("nodes") or []
        node = next((n for n in nodes if isinstance(n, dict) and n.get("id") == node_id), None)
        if node is None:
            raise NodeNotFound(node_id)
        spec = TYPES.get(node.get("type"))  # type: ignore[arg-type]
        if spec is None or node["type"] in ("start", "human", "end"):
            raise NotTestable("开始、人工和结束节点不能单独试跑")
        if node["type"] in ("databricks_ingest", "databricks_dev_run", "databricks_release"):
            raise NotTestable(f"{spec.label}节点会写 Databricks，不能单独试跑")
        if node["type"] == "data_check":
            raise NotTestable("数据检查节点要读这次运行的结果表和项目，不能单独试跑")
        if node["type"] == "app":
            raise NotTestable("调用应用节点会真的发起一次运行，不能单独试跑")
        if node["type"] == "git_pr":
            raise NotTestable("提交 PR 节点会真的在代码仓库开 PR，不能单独试跑")
        p = Problems(("config", "graph", "nodes", node_id))
        check_common(node, p)
        await spec.check(node, p, self._env(await self.models()))
        if p.found:
            raise ConfigError(p.found)
        rt = await self._runs.runtime({**config, "graph": {"nodes": [node], "edges": []}}, app)
        state: dict[str, Any] = {"input": {}, "vars": {}}
        for key, value in values.items():
            source, _, field = key.partition(".")
            state["vars"].setdefault(source, {})[field] = value
        get = rt.lookup(state)

        def shown(target: str, name: str) -> Any:
            return MASK if target == refs.SECRET else get(target, name)

        settings = {
            k: v for k, v in node.items() if k not in {"id", "type", "title", "ui"} | spec.verbatim
        }
        rendered = {
            ".".join(str(part) for part in loc): refs.render(text, shown)
            for loc, text in refs.strings(settings)
            if refs.find(text)
        }
        if node["type"] == "knowledge":
            rendered["query"] = refs.render(node["query"], shown)
        out: dict[str, Any] = {"outputs": None, "branch": None, "error": None, "rendered": rendered}
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(spec.run(node, state, rt), NODE_TEST_SECONDS)
            out |= {"status": "done", "outputs": outcome.outputs, "branch": outcome.branch}
        except NodeFailure as exc:
            out |= {"status": "error", "error": {"type": exc.type, "message": exc.message}}
        except TimeoutError:
            message = f"超过 {NODE_TEST_SECONDS} 秒没有结束"
            out |= {"status": "error", "error": {"type": "timeout", "message": message}}
        except Exception as exc:  # noqa: BLE001 - the person sees what went wrong
            kind = "tool_error" if spec.kind == "tool" else "model_error"
            message = str(exc) or type(exc).__name__
            out |= {"status": "error", "error": {"type": kind, "message": message}}
        out["duration_ms"] = round((time.monotonic() - started) * 1000)
        return out

    async def publish(self, app: str, note: str, actor: str | None) -> None:
        await self._check(app)
        draft = await self._runs.versions.draft(app)
        if draft is None:
            raise NotPublishable("没有草稿")
        if await self.has_eval_set(app):
            status = (draft.eval or {}).get("status")
            if status != "passed":
                reason = {
                    None: "草稿还没有跑评测",
                    "running": "评测还在进行",
                    "failed": "草稿没有通过评测",
                    "error": "评测出错了，请重新运行",
                }.get(status, "草稿没有通过评测")
                raise NotPublishable(f"{reason}，通过后才能发布")
        # Only the revision whose eval was checked above: a draft saved since then has not
        # been evaluated.
        await self._runs.versions.publish(app, note, actor, draft.revision)

    async def activate(self, app: str, version: int) -> None:
        await self._check(app)
        await self._runs.ensure_versions(app)
        await self._runs.versions.activate(app, version)

    # The eval gate

    async def start_eval(self, app: str) -> None:
        await self._check(app)
        # One start at a time per app, so two requests cannot both pass the check below.
        async with self._starting.setdefault(app, asyncio.Lock()):
            await self._start_eval(app)

    async def _start_eval(self, app: str) -> None:
        draft = await self._runs.versions.draft(app)
        if draft is None:
            raise EvalUnavailable("没有草稿")
        path = self.eval_set(app)
        judge_model: str | None = None
        if path is not None:
            if not path.is_file():
                raise EvalUnavailable(f"找不到评测集文件 {path}，请检查 AEP_EVALS_DIR")
            cases = evals.load_cases(path)
            threshold = self._settings.eval_publish_threshold
        else:
            found = await self.eval_sets.get(app)
            if found is None or not found.cases:
                raise EvalUnavailable("这个应用没有评测集")
            cases, threshold, judge_model = found.cases, found.threshold, found.judge_model
        running = self._evals.get(app)
        if running is not None and not running.done():
            raise EvalUnavailable("已经有评测在进行")
        state = {
            "status": "running",
            "score": None,
            "threshold": threshold,
            "case_count": len(cases),
            "started_at": _now(),
            "finished_at": None,
            "error": None,
            "cases": [
                {
                    "id": c["id"],
                    "title": c.get("title"),
                    "status": "pending",
                    "score": None,
                    "run_id": None,
                    "error": None,
                    "checks": [],
                }
                for c in cases
            ],
        }
        if path is None:
            state["fields"] = []
        await self._runs.versions.set_draft_eval(app, draft.revision, state)
        task = asyncio.create_task(
            self._evaluate(app, draft, cases, state, path is None, judge_model)
        )
        self._evals[app] = task

    async def recover(self) -> int:
        """Mark evals left running by the previous process as interrupted."""
        stopped = 0
        for app in await self._runs.app_names():
            draft = await self._runs.versions.draft(app)
            if draft and (draft.eval or {}).get("status") == "running":
                interrupted = draft.eval | {
                    "status": "error",
                    "error": "服务重启，评测中断了，请重新运行",
                    "finished_at": _now(),
                }
                await self._runs.versions.set_draft_eval(app, draft.revision, interrupted)
                stopped += 1
        return stopped

    async def wait_idle(self) -> None:
        """Wait for every eval started so far (tests)."""
        while any(not t.done() for t in self._evals.values()):
            await asyncio.gather(*self._evals.values(), return_exceptions=True)

    async def _datasource_id(self) -> str:
        """The source eval cases run on: AEP_EVAL_DATASOURCE (a name or id; a default
        source renamed since is found by its connector address), else the first data
        source. Tool services are never one."""
        sources = [s for s in await self._datasources.all() if s.kind in DATABASE_KINDS]
        if not sources:
            raise RuntimeError("平台里没有登记数据源，评测跑不了")
        wanted = self._settings.eval_datasource
        if not wanted:
            return sources[0].datasource_id
        for s in sources:
            if wanted in (s.name, s.datasource_id):
                return s.datasource_id
        for item in self._settings.default_datasources:
            if item.get("name") == wanted:
                url = item.get("connector_url", "")
                for s in sources:
                    if same_url(s.connector_url, url):
                        return s.datasource_id
        raise RuntimeError(f"找不到评测用的数据源 {wanted}（AEP_EVAL_DATASOURCE）")

    async def _evaluate(
        self,
        app: str,
        draft: Draft,
        cases: list[dict[str, Any]],
        state: dict[str, Any],
        flow: bool = False,
        judge_model: str | None = None,
    ) -> None:
        lock = asyncio.Lock()

        async def save() -> None:
            async with lock:
                await self._runs.versions.set_draft_eval(app, draft.revision, state)

        try:
            # Flow cases carry their whole input; the ETL ones run on a data source.
            datasource_id = "" if flow else await self._datasource_id()
            judge = self._judge(judge_model or self._runs.default_model)
            gate = asyncio.Semaphore(max(self._settings.eval_parallel, 1))

            async def one(i: int, case: dict[str, Any]) -> evals.CaseResult:
                async with gate:
                    latest = await self._runs.versions.draft(app)
                    if latest is None or latest.revision != draft.revision:
                        # The draft changed or went away: its result would not be kept.
                        return evals.CaseResult(case["id"], error="草稿已经改了")
                    state["cases"][i]["status"] = "running"
                    if flow:
                        result = await self._run_flow_case(app, draft, case, state, i, save, judge)
                    else:
                        result = await self._run_case(
                            app, draft, case, datasource_id, state, i, save
                        )
                    state["cases"][i] |= {
                        "status": "done" if result.status == "done" else "error",
                        "score": result.score,
                        "error": result.error,
                        "checks": [_check_out(c) for c in result.checks],
                    }
                    if flow:
                        state["fields"] = eval_sets.field_summary(state["cases"])
                    await save()
                    return result

            results = await asyncio.gather(*(one(i, c) for i, c in enumerate(cases)))
            total = evals.summary(list(results))["score"]
            state["score"] = total
            if total is None:
                state |= {"status": "error", "error": "没有可以打分的检查"}
            else:
                passed = total >= state["threshold"]
                state["status"] = "passed" if passed else "failed"
        except Exception as exc:
            log.exception("eval of the %s draft failed", app)
            state |= {"status": "error", "error": str(exc) or type(exc).__name__}
        state["finished_at"] = _now()
        await save()

    async def _run_case(
        self,
        app: str,
        draft: Draft,
        case: dict[str, Any],
        datasource_id: str,
        state: dict[str, Any],
        index: int,
        save: Any,
    ) -> evals.CaseResult:
        out = evals.CaseResult(case["id"])
        started = time.monotonic()
        deadline = started + self._settings.eval_timeout_seconds
        plan: dict[str, Any] | None = None
        run: dict[str, Any]
        try:
            inp = evals.case_input(case, datasource_id)
            record = await self._runs.create(app, inp, config=draft.config)
            out.run_id = record.run_id
            state["cases"][index]["run_id"] = record.run_id
            await save()
            run = await self._wait(record.run_id, deadline)
            plan = evals.waiting_plan(run)
            if plan is not None:
                await self._runs.resume(record.run_id, "approve", None, evals.ACTOR)
                run = await self._wait(record.run_id, deadline)
            if run["status"] == "waiting":
                run = {**run, "status": "error", "error": "run 停在了意外的确认步骤"}
        except Exception as exc:  # noqa: BLE001 - the case fails, the eval goes on
            out.error = str(exc) or type(exc).__name__
            run = {"status": "error", "error": out.error}
        out.status = run.get("status", "error")
        out.error = out.error or run.get("error")
        out.checks = evals.score_case(case, plan, run)
        out.seconds = round(time.monotonic() - started, 1)
        return out

    def _judge(self, model: str) -> eval_sets.Judge:
        """Asks ``model`` whether an answer meets a check's criteria."""
        rules = policy.platform_default(self._settings)

        async def judge(answer: str, criteria: str, reference: str | None) -> tuple[bool, str]:
            if not rules.allows(model):
                return False, f"平台不允许用模型 {model} 判断"
            try:
                reply = await llm.chat(
                    self._runs.llm_client,
                    eval_sets.judge_messages(answer, criteria, reference),
                    model=model,
                    temperature=0,
                )
                return eval_sets.judge_reply(llm.parse_json(reply.text))
            except Exception as exc:  # noqa: BLE001 - the check fails, saying why
                return False, f"模型判断没有成功：{str(exc) or type(exc).__name__}"

        return judge

    async def _run_flow_case(
        self,
        app: str,
        draft: Draft,
        case: dict[str, Any],
        state: dict[str, Any],
        index: int,
        save: Any,
        judge: eval_sets.Judge,
    ) -> evals.CaseResult:
        out = evals.CaseResult(case["id"])
        started = time.monotonic()
        deadline = started + self._settings.eval_timeout_seconds
        answers = case.get("answers") or {}
        run: dict[str, Any]
        try:
            record = await self._runs.create(
                app,
                case.get("input") or {},
                config=draft.config,
                requester=evals.ACTOR,
                seed=self._case_turn(app, draft, case),
            )
            out.run_id = record.run_id
            state["cases"][index]["run_id"] = record.run_id
            await save()
            run = await self._wait(record.run_id, deadline)
            asked = 0
            while run["status"] == "waiting":
                asked += 1
                if asked > eval_sets.MAX_ANSWERS:
                    raise RuntimeError(f"运行等人回答超过 {eval_sets.MAX_ANSWERS} 次")
                await self._answer(record.run_id, run["pending"] or {}, answers)
                run = await self._wait(record.run_id, deadline)
        except InvalidValues as exc:
            out.error = "输入不对：" + "；".join(exc.problems.values())
            run = {"status": "error", "error": out.error}
        except Exception as exc:  # noqa: BLE001 - the case fails, the eval goes on
            out.error = str(exc) or type(exc).__name__
            run = {"status": "error", "error": out.error}
        out.status = run.get("status", "error")
        out.error = out.error or run.get("error")
        out.checks = await eval_sets.score_flow_case(case, run, judge)
        out.seconds = round(time.monotonic() - started, 1)
        return out

    def _case_turn(self, app: str, draft: Draft, case: dict[str, Any]) -> dict[str, Any] | None:
        """A case with history runs as the next turn of a conversation that has it: the
        history as it is, the case's turn and the tokens it turned back; no conversation
        is kept (docs/api/evals.md, "history")."""
        history = case.get("history") or []
        if not history and case.get("turn") is None:
            return None
        if chat.chat_settings(self._runs.flow_config(app, draft.config)) is None:
            raise RuntimeError(NOT_CHAT)
        turn = case.get("turn") or len(history) + 1
        return {
            refs.CONVERSATION: {"id": None, "turn": turn, "history": copy.deepcopy(history)},
            "restored": dict(case.get("restored") or {}),
        }

    async def _answer(self, run_id: str, pending: dict[str, Any], answers: dict[str, Any]) -> None:
        """Answer the person's step the run waits at, as the case says."""
        node = (pending.get("details") or {}).get("node")
        given = answers.get(node) or {}
        title = pending.get("prompt") or node
        options = pending.get("options") or []
        if pending.get("kind") == "form":
            if given.get("values") is None:
                raise RuntimeError(f"用例没有给表单「{node}」的值")
            try:
                await self._runs.resume(run_id, "submit", None, evals.ACTOR, given["values"])
            except InvalidValues as exc:
                problems = "；".join(exc.problems.values())
                raise RuntimeError(f"表单「{node}」的值不对：{problems}") from exc
            return
        decision = given.get("decision") or (options[0] if options else None)
        try:
            await self._runs.resume(
                run_id, decision or "", given.get("comment"), evals.ACTOR, given.get("values")
            )
        except InvalidDecision as exc:
            raise RuntimeError(f"「{title}」没有选项 {decision}") from exc
        except CommentRequired as exc:
            raise RuntimeError(f"「{title}」选 {decision} 要写意见，用例里没写") from exc
        except InvalidValues as exc:
            problems = "；".join(exc.problems.values())
            raise RuntimeError(f"「{title}」的回答不对：{problems}") from exc

    # Eval sets (docs/api/evals.md)

    async def get_eval_set(self, app: str) -> dict[str, Any]:
        kind = await self._check(app)
        path = self.eval_set(app)
        if path is not None:
            cases = evals.load_cases(path) if path.is_file() else []
            return {
                "app": app,
                "source": "file",
                "editable": False,
                "threshold": self._settings.eval_publish_threshold,
                "judge_model": None,
                "cases": cases,
                "updated_by": None,
                "updated_at": None,
            }
        found = await self.eval_sets.get(app)
        return {
            "app": app,
            "source": "platform" if found else None,
            "editable": kind == "flow",
            "threshold": found.threshold if found else self._settings.eval_publish_threshold,
            "judge_model": found.judge_model if found else None,
            "cases": found.cases if found else [],
            "updated_by": found.updated_by if found else None,
            "updated_at": found.updated_at.isoformat() if found and found.updated_at else None,
        }

    async def _editable(self, app: str) -> None:
        if await self._check(app) != "flow" or self.eval_set(app) is not None:
            raise EvalSetReadOnly(app)

    async def put_eval_set(
        self,
        app: str,
        cases: list[dict[str, Any]],
        threshold: float | None,
        judge_model: str | None,
        actor: str | None,
    ) -> dict[str, Any]:
        await self._editable(app)
        talking = [i for i, c in enumerate(cases) if c.get("history") or c.get("turn") is not None]
        if talking and not await self._converses(app):
            raise ConfigError([(["cases", i, "history"], NOT_CHAT) for i in talking])
        found = await self.eval_sets.get(app)
        await self.eval_sets.put(
            eval_sets.EvalSet(
                app,
                threshold
                if threshold is not None
                else (found.threshold if found else self._settings.eval_publish_threshold),
                judge_model,
                cases,
                actor,
                datetime.now(UTC),
            )
        )
        return await self.get_eval_set(app)

    async def _converses(self, app: str) -> bool:
        """Whether the app's current version or its draft converses."""
        store = self._runs.versions
        await self._runs.ensure_versions(app)
        found = [await store.version(app, await store.current(app)), await store.draft(app)]
        return any(
            v is not None and chat.chat_settings(self._runs.flow_config(app, v.config))
            for v in found
        )

    async def case_from_run(
        self, app: str, run_id: str, rating: str, actor: str | None
    ) -> dict[str, Any]:
        await self._editable(app)
        try:
            view = await self._runs.get(run_id)
        except RunNotFound as exc:
            raise CaseFromRunProblem("没有找到这次运行", 404) from exc
        if view.record.app != app:
            raise CaseFromRunProblem(f"这次运行不是应用 {app} 的")
        if view.record.status not in ("done", "error"):
            raise CaseFromRunProblem("运行结束后才能存成用例")
        found = await self.eval_sets.get(app) or eval_sets.EvalSet(
            app, self._settings.eval_publish_threshold
        )
        if len(found.cases) >= eval_sets.MAX_CASES:
            raise CaseFromRunProblem(f"一个评测集最多 {eval_sets.MAX_CASES} 条用例")
        try:
            turn = await self._runs.turn_context(run_id)
        except TurnCleared as exc:
            raise CaseFromRunProblem("这次运行属于已过期的会话，内容已清除") from exc
        good = rating == "good" and view.record.status == "done"
        # How the run went (an agent's steps and hits) changes every run: no checks for it.
        # What a model wrote is worded differently every run: a judge, not equals. Only
        # when the run sent data as it is: otherwise the judge would be shown values the
        # model never saw (masked ones turned back), so those stay with equals.
        flow = self._runs.flow_config(app, view.record.config or {})
        skip = flowgraph.execution_outputs(flow)
        samples = (view.record.policy or {}).get("samples", "full")
        worded = flowgraph.model_text_outputs(flow) if samples == "full" else set()
        case = {
            "id": eval_sets.case_id(run_id, {c["id"] for c in found.cases}),
            "title": self._runs.title(view.record),
            "input": view.record.input,
            "answers": await self._runs.answers_given(run_id),
            "expect": eval_sets.suggested_checks(view.result, skip, worded) if good else [],
            # A run outside a conversation replays as one: no history, turn 0.
            **(turn or {"history": [], "turn": None, "restored": {}}),
            "from_run": run_id,
        }
        if case["title"] and len(case["title"]) > 100:
            case["title"] = case["title"][:99] + "…"
        found.cases.append(case)
        found.updated_by, found.updated_at = actor, datetime.now(UTC)
        await self.eval_sets.put(found)
        return case

    async def cases_from(self, app: str, run_id: str) -> list[str]:
        """The ids of the cases saved from this run."""
        found = await self.eval_sets.get(app)
        return [c["id"] for c in (found.cases if found else []) if c.get("from_run") == run_id]

    async def _wait(self, run_id: str, deadline: float) -> dict[str, Any]:
        while True:
            view: RunView = await self._runs.get(run_id)
            if view.record.status == "cancelled":
                # Someone cancelled the case's run: the case fails now, not at the timeout.
                reason = view.record.error
                error = f"run 被取消了：{reason}" if reason else "run 被取消了"
                return {"status": "error", "error": error, "pending": None, "result": None}
            if view.record.status == "waiting" or view.record.status in FINISHED:
                return {
                    "status": view.record.status,
                    "error": view.record.error,
                    "pending": view.pending,
                    "result": view.result,
                }
            if time.monotonic() > deadline:
                raise TimeoutError(f"run {run_id} 超时没有结束")
            await asyncio.sleep(POLL_SECONDS)
