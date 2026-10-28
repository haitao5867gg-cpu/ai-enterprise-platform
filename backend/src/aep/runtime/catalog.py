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
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from aep import evals
from aep.config import Settings
from aep.datasources import DataSourceStore
from aep.runtime import appconfig, design
from aep.runtime.appconfig import ConfigError
from aep.runtime.flow import builtin as flowbuiltin
from aep.runtime.flow import files as flowfiles
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow import refs
from aep.runtime.flow.check import check_common, check_config
from aep.runtime.flow.nodes import TYPES, CheckEnv, NodeFailure, Problems
from aep.runtime.flow.store import AppExists
from aep.runtime.service import RunService, RunView
from aep.runtime.versions import AppVersion, Draft, VersionNotFound
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


# How long a node tried on its own may take.
NODE_TEST_SECONDS = 120
MASK = "******"


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    ) -> None:
        self._runs = runs
        self._datasources = datasources
        self._settings = settings
        self._models = models
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

    async def models(self) -> list[str] | None:
        return await self._models() if self._models else None

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
            kind = "flow"
        return {
            "app": app,
            "kind": kind,
            "title": title,
            "summary": summary,
            "created_at": created_at,
            "current_version": await self._runs.versions.current(app),
            "has_draft": await self._runs.versions.draft(app) is not None,
            "has_eval_set": self.eval_set(app) is not None,
        }

    def _config(self, app: str, config: dict[str, Any]) -> dict[str, Any]:
        return config if app in self._runs.code_apps else self._runs.flow_config(app, config)

    def _version_out(self, v: AppVersion, current: int) -> dict[str, Any]:
        return {
            "version": v.version,
            "status": "current" if v.version == current else "previous",
            "config": self._config(v.app, v.config),
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
        else:
            config = self._runs.flow_config(app, current.config)
            options = {}
            nodes, edges = flowgraph.shape(config, await self._runs.runtime(config))
            texts = {
                "pattern": flowgraph.DESIGN["pattern"],
                "knowledge": flowgraph.DESIGN["knowledge"],
                "skills_text": flowgraph.DESIGN["skills"],
            }
            inputs = flowgraph.inputs(config)
        skills: list[Skill] = await self._runs.skills.all()
        models = await self.models()
        default = self._runs.default_model
        return (
            summary
            | texts
            | {
                "inputs": inputs,
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
                },
            }
        )

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

    # Changing

    async def save_draft(self, app: str, raw: Any, actor: str | None) -> None:
        kind = await self._check(app)
        await self._runs.ensure_versions(app)
        if kind == "flow":
            config = await self.check_flow(raw)
        else:
            config = await appconfig.validate_config(
                raw, self._runs.options[app], self._runs.skills, await self.models()
            )
        await self._runs.versions.save_draft(app, config, actor)

    def _env(self, models: list[str] | None, strict: bool = True) -> CheckEnv:
        return CheckEnv(models, self._runs.skills, self._datasources, self._runs.tools, strict)

    async def check_flow(self, raw: Any, strict: bool = True) -> dict[str, Any]:
        return await check_config(raw, self._env(await self.models(), strict))

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
        """A new flow app; its version 1 is a blank app, a template or a copy of a flow
        app's version."""
        if app in self._runs.code_apps or await self._runs.flows.get(app) is not None:
            raise AppExists(app)
        if source.get("template"):
            template = flowbuiltin.TEMPLATES.get(source["template"])
            if template is None:
                raise BadSource(f"没有模板 {source['template']}")
            raw, note = template.config, f"从模板「{template.title}」新建"
        elif source.get("app"):
            original = source["app"]
            if await self._runs.kind(original) != "flow":
                raise BadSource(f"没有编排应用 {original}，代码应用不能复制")
            await self._runs.ensure_versions(original)
            number = source.get("version") or await self._runs.versions.current(original)
            found = await self._runs.versions.version(original, number)
            if found is None:
                raise BadSource(f"应用 {original} 没有版本 {number}")
            raw = self._runs.flow_config(original, found.config)
            note = f"复制自 {original} 版本 {number}"
        else:
            raw, note = flowbuiltin.blank(), "新建空白应用"
        config = await self.check_flow(raw)
        await self._runs.flows.create(app, title, summary, actor)
        await self._runs.versions.ensure(app, config, note, actor)

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
        config = self._runs.flow_config(app, found.config)
        text = flowfiles.dump(flow.title, flow.summary, app, version, config, sources)
        return f"{app}-v{version}.yaml", text

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
        models = await self.models()
        if models is not None:
            for model in dict.fromkeys(
                n["model"] for n in flowgraph.nodes(config) if n.get("model")
            ):
                if model not in models:
                    out.append(
                        {"kind": "model", "name": model, "message": "模型网关上没有这个别名"}
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
        config = await self.check_flow(data["config"], strict=False)
        problems = await self._missing(app, config, data["requires"])
        name = (title or data["title"] or app)[:50]
        await self._runs.flows.create(app, name, (summary or data["summary"])[:500], actor)
        if any(p["kind"] != "secret" for p in problems):
            blank = await self.check_flow(flowbuiltin.blank())
            await self._runs.versions.ensure(
                app, blank, "新建空白应用（导入的配置在草稿里）", actor
            )
            await self._runs.versions.save_draft(app, config, actor)
        else:
            await self._runs.versions.ensure(app, config, "从文件导入", actor)
        return problems

    async def import_draft(self, app: str, text: str, actor: str | None) -> list[dict[str, Any]]:
        """A file imported as the draft of an existing flow app."""
        await self._flow(app)
        data = flowfiles.load(text)
        config = await self.check_flow(data["config"], strict=False)
        problems = await self._missing(app, config, data["requires"])
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
        if self.eval_set(app) is not None:
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
        if path is None:
            raise EvalUnavailable("这个应用没有评测集")
        if not path.is_file():
            raise EvalUnavailable(f"找不到评测集文件 {path}，请检查 AEP_EVALS_DIR")
        running = self._evals.get(app)
        if running is not None and not running.done():
            raise EvalUnavailable("已经有评测在进行")
        cases = evals.load_cases(path)
        state = {
            "status": "running",
            "score": None,
            "threshold": self._settings.eval_publish_threshold,
            "started_at": _now(),
            "finished_at": None,
            "error": None,
            "cases": [
                {"id": c["id"], "status": "pending", "score": None, "run_id": None, "error": None}
                for c in cases
            ],
        }
        await self._runs.versions.set_draft_eval(app, draft.revision, state)
        task = asyncio.create_task(self._evaluate(app, draft, cases, state))
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
        sources = await self._datasources.all()
        if not sources:
            raise RuntimeError("平台里没有登记数据源，评测跑不了")
        wanted = self._settings.eval_datasource
        if not wanted:
            return sources[0].datasource_id
        for s in sources:
            if wanted in (s.name, s.datasource_id):
                return s.datasource_id
        raise RuntimeError(f"找不到评测用的数据源 {wanted}（AEP_EVAL_DATASOURCE）")

    async def _evaluate(
        self, app: str, draft: Draft, cases: list[dict[str, Any]], state: dict[str, Any]
    ) -> None:
        lock = asyncio.Lock()

        async def save() -> None:
            async with lock:
                await self._runs.versions.set_draft_eval(app, draft.revision, state)

        try:
            datasource_id = await self._datasource_id()
            gate = asyncio.Semaphore(max(self._settings.eval_parallel, 1))

            async def one(i: int, case: dict[str, Any]) -> evals.CaseResult:
                async with gate:
                    latest = await self._runs.versions.draft(app)
                    if latest is None or latest.revision != draft.revision:
                        # The draft changed or went away: its result would not be kept.
                        return evals.CaseResult(case["id"], error="草稿已经改了")
                    state["cases"][i]["status"] = "running"
                    result = await self._run_case(app, draft, case, datasource_id, state, i, save)
                    state["cases"][i] |= {
                        "status": "done" if result.status == "done" else "error",
                        "score": result.score,
                        "error": result.error,
                    }
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

    async def _wait(self, run_id: str, deadline: float) -> dict[str, Any]:
        while True:
            view: RunView = await self._runs.get(run_id)
            if view.record.status in ("waiting", "done", "error"):
                return {
                    "status": view.record.status,
                    "error": view.record.error,
                    "pending": view.pending,
                    "result": view.result,
                }
            if time.monotonic() > deadline:
                raise TimeoutError(f"run {run_id} 超时没有结束")
            await asyncio.sleep(POLL_SECONDS)
