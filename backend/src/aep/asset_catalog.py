"""The asset catalog (docs/api/catalog.md): the platform's assets in one list, with an owner
and tags, the apps that use each, a quality score from records the platform already keeps,
and a way to propose one to the assets repository as a pull request.

It creates no assets: templates, apps, skills, knowledge bases, data sources and eval sets
stay where they are; only the owner, tags and last pull request live here."""

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.assets import AssetRepo
from aep.config import Settings
from aep.datasources import DataSourceStore, canon_id
from aep.knowledge import governance
from aep.knowledge.extract import ExtractError, read_text
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import builtin as flowbuiltin
from aep.runtime.flow import files as flowfiles
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow import refs
from aep.runtime.store import DEFAULT_TENANT

log = logging.getLogger(__name__)

Kind = Literal["template", "app", "skill", "knowledge", "tool", "eval_set"]
KINDS: tuple[Kind, ...] = ("template", "app", "skill", "knowledge", "tool", "eval_set")
LABELS: dict[str, str] = {
    "template": "应用模板",
    "app": "应用",
    "skill": "Skill",
    "knowledge": "知识库",
    "tool": "数据源和工具",
    "eval_set": "评测集",
}
MAX_TAGS = 10
MAX_TAG = 20
MAX_OWNER = 50
MAX_NOTE = 500
MIN_RUNS = 3  # runs before a success rate counts
WINDOW = timedelta(days=30)
TEMPLATE_NOTE = re.compile(r"^从模板「(.+)」新建$")


class NotFound(LookupError):
    """No such asset."""


class Refused(Exception):
    """The asset cannot be proposed; the message says why."""


class NoRepo(Exception):
    """The assets repository is not configured."""


@dataclass
class AssetMeta:
    kind: str
    asset_id: str
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    repo: dict[str, Any] | None = None
    updated_by: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AssetMetaStore(Protocol):
    async def setup(self) -> None: ...
    async def all(self) -> list[AssetMeta]: ...
    async def put(self, meta: AssetMeta) -> None: ...
    async def delete(self, kind: str, asset_id: str) -> None: ...


class MemoryAssetMetaStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], AssetMeta] = {}

    async def setup(self) -> None:
        return None

    async def all(self) -> list[AssetMeta]:
        return [replace(m, tags=list(m.tags)) for m in self.items.values()]

    async def put(self, meta: AssetMeta) -> None:
        self.items[(meta.kind, meta.asset_id)] = replace(meta, tags=list(meta.tags))

    async def delete(self, kind: str, asset_id: str) -> None:
        self.items.pop((kind, asset_id), None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_meta (
    tenant_id   text NOT NULL DEFAULT 'default',
    kind        text NOT NULL,
    asset_id    text NOT NULL,
    owner       text,
    tags        jsonb NOT NULL DEFAULT '[]',
    repo        jsonb,
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, kind, asset_id)
)"""


class PostgresAssetMetaStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def all(self) -> list[AssetMeta]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT kind, asset_id, owner, tags, repo, updated_by, updated_at"
                " FROM asset_meta WHERE tenant_id = %s",
                (DEFAULT_TENANT,),
            )
            return [AssetMeta(**row) for row in await cur.fetchall()]

    async def put(self, meta: AssetMeta) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO asset_meta"
                " (tenant_id, kind, asset_id, owner, tags, repo, updated_by, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, kind, asset_id) DO UPDATE SET owner = excluded.owner,"
                " tags = excluded.tags, repo = excluded.repo, updated_by = excluded.updated_by,"
                " updated_at = excluded.updated_at",
                (
                    DEFAULT_TENANT,
                    meta.kind,
                    meta.asset_id,
                    meta.owner,
                    Jsonb(meta.tags),
                    Jsonb(meta.repo) if meta.repo is not None else None,
                    meta.updated_by,
                    meta.updated_at,
                ),
            )

    async def delete(self, kind: str, asset_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM asset_meta WHERE tenant_id = %s AND kind = %s AND asset_id = %s",
                (DEFAULT_TENANT, kind, asset_id),
            )


def check_tags(tags: Iterable[str]) -> list[str]:
    """Trimmed, without repeats; ValueError says what is wrong."""
    found: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if not tag or len(tag) > MAX_TAG:
            raise ValueError(f"每个标签 1–{MAX_TAG} 个字符")
        if tag not in found:
            found.append(tag)
    if len(found) > MAX_TAGS:
        raise ValueError(f"标签最多 {MAX_TAGS} 个")
    return found


@dataclass
class _Use:
    app: str
    title: str
    version: int
    via: list[str] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        via = "、".join(self.via)
        return {"app": self.app, "title": self.title, "version": self.version, "via": via}


@dataclass
class _AppInfo:
    app: str
    kind: str
    title: str
    summary: str
    version: int | None
    config: dict[str, Any] | None
    eval: dict[str, Any] | None
    created_by: str | None
    created_at: datetime | None
    updated_at: datetime | None
    template: str | None  # the template title it was made from


@dataclass
class _Scan:
    """What one pass over the apps' current versions found."""

    apps: dict[str, _AppInfo] = field(default_factory=dict)
    uses: dict[tuple[str, str], dict[str, _Use]] = field(default_factory=dict)
    outcomes: dict[str, tuple[int, int]] = field(default_factory=dict)

    def use(self, kind: str, asset_id: str, info: _AppInfo, via: str) -> None:
        assert info.version is not None
        per_app = self.uses.setdefault((kind, asset_id), {})
        found = per_app.setdefault(info.app, _Use(info.app, info.title, info.version))
        if via and via not in found.via:
            found.via.append(via)

    def used_by(self, kind: str, asset_id: str) -> list[dict[str, Any]]:
        found = self.uses.get((kind, asset_id), {})
        return [found[app].out() for app in sorted(found)]

    def success(self, app: str) -> float | None:
        done, error = self.outcomes.get(app, (0, 0))
        return done / (done + error) if done + error >= MIN_RUNS else None


def _score(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _quality(score: float | None, basis: str, detail: str = "") -> dict[str, Any]:
    rounded = round(score, 4) if score is not None else None
    return {"score": rounded, "basis": basis, "detail": detail}


class AssetCatalog:
    def __init__(
        self,
        apps: AppCatalog,
        datasources: DataSourceStore,
        meta: AssetMetaStore,
        repo: AssetRepo | None,
        settings: Settings,
    ) -> None:
        self._apps = apps
        self._runs = apps.runs
        self._datasources = datasources
        self._meta = meta
        self._repo = repo
        self._settings = settings

    @property
    def repo_ready(self) -> bool:
        return self._repo is not None

    # Reading

    async def _app_info(self, app: str) -> _AppInfo | None:
        runs = self._runs
        await runs.ensure_versions(app)
        number = await runs.versions.current(app)
        current = await runs.versions.version(app, number)
        code = app in runs.code_apps
        if code:
            design = runs.app_design(app)
            title, summary = (design.title, design.summary) if design else (app, "")
            created_by, created_at = None, None
        else:
            flow = await runs.flows.get(app)
            if flow is None or flow.archived_at is not None:
                return None
            title, summary = flow.title, flow.summary
            created_by, created_at = flow.created_by, flow.created_at
        template = None
        if not code:
            first = current if number == 1 else await runs.versions.version(app, 1)
            matched = TEMPLATE_NOTE.match((first.note or "") if first else "")
            template = matched.group(1) if matched else None
        return _AppInfo(
            app=app,
            kind="code" if code else "flow",
            title=title,
            summary=summary,
            version=number if current else None,
            config=self._apps.config_of(app, current.config) if current else None,
            eval=current.eval if current else None,
            created_by=created_by,
            created_at=created_at,
            updated_at=current.created_at if current else created_at,
            template=template,
        )

    async def _scan(self) -> _Scan:
        scan = _Scan()
        for app in await self._runs.app_names():
            try:
                info = await self._app_info(app)
            except Exception:  # noqa: BLE001 - one broken app must not hide the others
                log.warning("could not read app %s for the catalog", app, exc_info=True)
                continue
            if info is None:
                continue
            scan.apps[app] = info
            if info.config is None:
                continue
            try:
                self._uses(scan, info)
            except Exception:  # noqa: BLE001
                log.warning("could not read what app %s uses", app, exc_info=True)
        templates = {t.title: t.id for t in flowbuiltin.TEMPLATES.values()}
        for info in scan.apps.values():
            if info.template in templates and info.version is not None:
                scan.use("template", templates[info.template], info, "从模板新建")
        scan.outcomes = await self._runs.outcomes(datetime.now(UTC) - WINDOW)
        return scan

    def _uses(self, scan: _Scan, info: _AppInfo) -> None:
        config = info.config or {}
        if info.kind == "code":
            for node_id, settings in (config.get("nodes") or {}).items():
                for ref in settings.get("skills") or []:
                    scan.use("skill", ref["name"], info, node_id)
                params = settings.get("params") or {}
                for kb_id in params.get("knowledge_bases") or []:
                    scan.use("knowledge", kb_id, info, node_id)
            return
        nodes = flowgraph.nodes(config)
        consts = {
            v.get("key"): v.get("value")
            for v in ((config.get("env") or {}).get("vars") or [])
            if isinstance(v, dict)
        }
        for node in nodes:
            title = str(node.get("title") or node["id"])
            for ref in node.get("skills") or []:
                scan.use("skill", ref["name"], info, title)
            if node["type"] in ("knowledge", "agent"):
                settings = node if node["type"] == "knowledge" else node.get("knowledge") or {}
                for kb_id in settings.get("knowledge_bases") or []:
                    scan.use("knowledge", kb_id, info, title)
            if node["type"] == "app" and isinstance(node.get("app"), str) and node["app"]:
                scan.use("app", node["app"], info, title)
            values: list[Any] = []
            if node["type"] in flowfiles.DATASOURCE_NODES:
                values = [node.get("datasource")]
            elif node["type"] == "agent":
                values = [t.get("datasource") for t in node.get("tools") or []]
            for value in values:
                if not isinstance(value, str):
                    continue
                ref = refs.whole(value)
                if ref is not None and ref[0] == refs.ENV:
                    value = consts.get(ref[1])
                if isinstance(value, str) and value.strip() and "{{" not in value:
                    scan.use("tool", canon_id(value), info, title)
        for datasource_id, via in flowfiles.datasource_uses(config).items():
            if via == "default_input":
                scan.use("tool", datasource_id, info, "开始节点的默认值")

    async def _meta_map(self, live: set[tuple[str, str]]) -> dict[tuple[str, str], AssetMeta]:
        found: dict[tuple[str, str], AssetMeta] = {}
        for meta in await self._meta.all():
            key = (meta.kind, meta.asset_id)
            if key in live:
                found[key] = meta
            else:  # the asset is gone: so is what the catalog said about it
                await self._meta.delete(*key)
        return found

    async def _items(self) -> list[dict[str, Any]]:
        scan = await self._scan()
        raw: list[dict[str, Any]] = []
        raw += self._templates(scan)
        raw += self._app_items(scan)
        raw += await self._skill_items(scan)
        raw += await self._knowledge_items(scan)
        raw += await self._tool_items(scan)
        raw += await self._eval_items(scan)
        metas = await self._meta_map({(item["kind"], item["id"]) for item in raw})
        out = []
        for item in raw:
            meta = metas.get((item["kind"], item["id"]))
            creator = item.pop("_creator", None)
            owner = meta.owner if meta and meta.owner else creator
            out.append(
                {
                    **item,
                    "owner": owner or None,
                    "owner_set": bool(meta and meta.owner),
                    "tags": list(meta.tags) if meta else [],
                    "repo": meta.repo if meta else None,
                }
            )
        order = {kind: i for i, kind in enumerate(KINDS)}
        out.sort(key=lambda item: (order[item["kind"]], item["title"], item["id"]))
        return out

    def _item(
        self,
        kind: Kind,
        asset_id: str,
        title: str,
        summary: str,
        link: str,
        scan: _Scan,
        *,
        version: int | None = None,
        updated_at: datetime | None = None,
        quality: dict[str, Any],
        facts: dict[str, Any],
        creator: str | None = None,
        can_propose: bool = False,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "id": asset_id,
            "title": title,
            "summary": summary,
            "link": link,
            "version": version,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "used_by": scan.used_by(kind, asset_id),
            "quality": quality,
            "facts": facts,
            "can_propose": can_propose,
            "_creator": creator,
        }

    def _templates(self, scan: _Scan) -> list[dict[str, Any]]:
        return [
            self._item(
                "template",
                t.id,
                t.title,
                t.summary,
                "/apps",
                scan,
                quality=_quality(None, "模板本身不评测"),
                facts={"needs": list(t.needs)},
                creator="平台内置",
            )
            for t in flowbuiltin.TEMPLATES.values()
        ]

    def _app_quality(self, info: _AppInfo, scan: _Scan) -> dict[str, Any]:
        score = _score((info.eval or {}).get("score"))
        if score is not None:
            cases = (info.eval or {}).get("case_count")
            detail = f"{cases} 条用例" if isinstance(cases, int) else ""
            return _quality(score, "评测通过率", detail)
        rate = scan.success(info.app)
        if rate is not None:
            done, error = scan.outcomes.get(info.app, (0, 0))
            return _quality(rate, "近 30 天运行成功率", f"成功 {done} 次，失败 {error} 次")
        return _quality(None, "还没有评测，近 30 天运行不到 3 次")

    def _app_items(self, scan: _Scan) -> list[dict[str, Any]]:
        out = []
        for info in scan.apps.values():
            done, error = scan.outcomes.get(info.app, (0, 0))
            rate = scan.success(info.app)
            out.append(
                self._item(
                    "app",
                    info.app,
                    info.title,
                    info.summary,
                    f"/apps/{info.app}",
                    scan,
                    version=info.version,
                    updated_at=info.updated_at,
                    quality=self._app_quality(info, scan),
                    facts={
                        "kind": info.kind,
                        "runs_30d": done + error,
                        "success_rate": round(rate, 4) if rate is not None else None,
                    },
                    creator=info.created_by,
                    can_propose=info.kind == "flow",
                )
            )
        return out

    async def _skill_items(self, scan: _Scan) -> list[dict[str, Any]]:
        out = []
        for skill in await self._runs.skills.all():
            scores = []
            for use in scan.used_by("skill", skill.name):
                score = _score((scan.apps[use["app"]].eval or {}).get("score"))
                if score is not None:
                    scores.append((use["app"], score))
            quality = (
                _quality(
                    sum(s for _, s in scores) / len(scores),
                    f"{len(scores)} 个应用的评测平均分",
                    "、".join(f"{app} {score:.2f}" for app, score in scores),
                )
                if scores
                else _quality(None, "用到它的应用还没有评测")
            )
            first = skill.versions[-1] if skill.versions else skill.draft
            out.append(
                self._item(
                    "skill",
                    skill.name,
                    skill.name,
                    skill.description,
                    f"/skills/{skill.name}",
                    scan,
                    version=skill.published_version,
                    updated_at=skill.updated_at,
                    quality=quality,
                    facts={"versions": len(skill.versions)},
                    creator=first.created_by if first else None,
                    can_propose=True,
                )
            )
        return out

    async def _knowledge_items(self, scan: _Scan) -> list[dict[str, Any]]:
        out = []
        store = self._runs.knowledge
        for kb in await store.all():
            total = kb.document_count
            ok = kb.status_counts.get("ready", 0)
            failed = kb.status_counts.get("error", 0)
            detail = f"{total} 个文档" + (f"，{failed} 个处理失败" if failed else "")
            quality = _quality(ok / total if total else None, "文档处理成功率", detail)
            if regressed := governance.quality(await store.get_regression(kb.kb_id), kb):
                quality = _quality(regressed[0], "检索回归通过率", regressed[1])
            out.append(
                self._item(
                    "knowledge",
                    kb.kb_id,
                    kb.name,
                    kb.description,
                    f"/knowledge/{kb.kb_id}",
                    scan,
                    updated_at=kb.updated_at,
                    quality=quality,
                    facts={
                        "documents": total,
                        "chunks": kb.chunk_count,
                        "embedding_model": kb.embedding_model,
                    },
                    can_propose=True,
                )
            )
        return out

    async def _tool_items(self, scan: _Scan) -> list[dict[str, Any]]:
        out = []
        for source in await self._datasources.all():
            asset_id = canon_id(source.datasource_id)
            rates = [
                rate
                for use in scan.used_by("tool", asset_id)
                if (rate := scan.success(use["app"])) is not None
            ]
            quality = (
                _quality(
                    sum(rates) / len(rates),
                    "用到它的应用的运行成功率",
                    f"{len(rates)} 个应用近 30 天的平均",
                )
                if rates
                else _quality(None, "用到它的应用近 30 天运行不到 3 次")
            )
            out.append(
                self._item(
                    "tool",
                    asset_id,
                    source.name,
                    source.description,
                    "/datasources",
                    scan,
                    updated_at=source.created_at,
                    quality=quality,
                    facts={"type": source.kind},
                )
            )
        return out

    async def _eval_items(self, scan: _Scan) -> list[dict[str, Any]]:
        out = []
        for info in scan.apps.values():
            found = await self._apps.get_eval_set(info.app)
            if not found["cases"]:
                continue
            score = _score((info.eval or {}).get("score"))
            quality = (
                _quality(score, "最近一次评测通过率", f"门槛 {found['threshold']}")
                if score is not None
                else _quality(None, "当前版本还没有评测")
            )
            updated = found["updated_at"]
            out.append(
                {
                    **self._item(
                        "eval_set",
                        info.app,
                        f"{info.title}的评测集",
                        f"{len(found['cases'])} 条用例",
                        f"/apps/{info.app}#release",
                        scan,
                        quality=quality,
                        facts={"cases": len(found["cases"]), "threshold": found["threshold"]},
                        creator=found["updated_by"],
                        can_propose=found["source"] == "platform",
                    ),
                    "updated_at": updated,
                    "used_by": [
                        {
                            "app": info.app,
                            "title": info.title,
                            "version": info.version,
                            "via": "评测集",
                        }
                    ],
                }
            )
        return out

    async def items(self, kind: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        found = await self._items()
        if kind:
            found = [item for item in found if item["kind"] == kind]
        if q and q.strip():
            needle = q.strip().casefold()
            found = [
                item
                for item in found
                if any(
                    needle in str(text).casefold()
                    for text in (item["title"], item["summary"], item["owner"] or "", *item["tags"])
                )
            ]
        return found

    async def item(self, kind: str, asset_id: str) -> dict[str, Any]:
        if kind not in KINDS:
            raise NotFound(kind)
        for found in await self.items(kind):
            if found["id"] == asset_id:
                return found
        raise NotFound(asset_id)

    # Changing

    async def _stored(self, kind: str, asset_id: str) -> AssetMeta:
        for meta in await self._meta.all():
            if (meta.kind, meta.asset_id) == (kind, asset_id):
                return meta
        return AssetMeta(kind, asset_id)

    async def set_meta(
        self, kind: str, asset_id: str, owner: str | None, tags: list[str], actor: str | None
    ) -> dict[str, Any]:
        await self.item(kind, asset_id)
        meta = await self._stored(kind, asset_id)
        meta.owner = owner.strip() if owner and owner.strip() else None
        meta.tags = check_tags(tags)
        meta.updated_by, meta.updated_at = actor, datetime.now(UTC)
        await self._meta.put(meta)
        return await self.item(kind, asset_id)

    async def propose(
        self, kind: str, asset_id: str, note: str, actor: str | None
    ) -> dict[str, Any]:
        item = await self.item(kind, asset_id)
        label = LABELS[kind]
        if not item["can_propose"]:
            if kind == "app" and item["facts"]["kind"] == "code":
                raise Refused(f"「{item['title']}」是代码应用，不提交到资产仓库")
            if kind == "eval_set":
                raise Refused(f"「{item['title']}」放在代码里，不提交到资产仓库")
            raise Refused(f"{label}不提交到资产仓库")
        if kind in ("app", "skill") and item["version"] is None:
            raise Refused(f"「{item['title']}」还没有发布的版本")
        if self._repo is None:
            raise NoRepo()
        directory, files = await self._files(kind, asset_id, item)
        branch = f"aep/catalog/{kind}/{asset_id}"
        title = f"资产：{label}「{item['title']}」"
        lines = [f"由平台的资产目录提交：{self._settings.web_url.rstrip('/')}/catalog"]
        if item["version"] is not None:
            lines.append(f"版本：v{item['version']}")
        if item["owner"]:
            lines.append(f"负责人：{item['owner']}")
        if actor:
            lines.append(f"提交的人：{actor}")
        if note.strip():
            lines += ["", note.strip()]
        published = await self._repo.open_pr(
            directory, files, branch, title, "\n".join(lines), title
        )
        meta = await self._stored(kind, asset_id)
        meta.repo = {
            "number": published.number,
            "url": published.url,
            "at": datetime.now(UTC).isoformat(),
            "by": actor,
        }
        meta.updated_by, meta.updated_at = actor, datetime.now(UTC)
        await self._meta.put(meta)
        return {
            "number": published.number,
            "url": published.url,
            "branch": published.branch,
            "created": published.created,
            "changed": list(published.changed),
        }

    async def _files(
        self, kind: str, asset_id: str, item: dict[str, Any]
    ) -> tuple[str, dict[str, str]]:
        if kind == "app":
            _, text = await self._apps.export(asset_id, item["version"])
            files = {"app.yaml": text}
            found = await self._apps.get_eval_set(asset_id)
            if found["source"] == "platform" and found["cases"]:
                files["eval-set.yaml"] = _eval_yaml(found)
            return f"apps/{asset_id}", files
        if kind == "eval_set":
            return f"evals/{asset_id}", {
                "eval-set.yaml": _eval_yaml(await self._apps.get_eval_set(asset_id))
            }
        if kind == "skill":
            skill = await self._runs.skills.get(asset_id)
            if skill is None or skill.current is None:
                raise NotFound(asset_id)
            current = skill.current
            head = yaml.safe_dump(
                {
                    "name": skill.name,
                    "description": current.description,
                    "version": current.version,
                },
                allow_unicode=True,
                sort_keys=False,
            )
            files = {"SKILL.md": f"---\n{head}---\n\n{current.body.strip()}\n"}
            for name, text in current.references.items():
                files[f"references/{name}"] = text
            return f"skills/{asset_id}", files
        return f"knowledge/{asset_id}", await self._knowledge_files(asset_id)

    async def _knowledge_files(self, kb_id: str) -> dict[str, str]:
        store = self._runs.knowledge
        kb = await store.get(kb_id)
        if kb is None:
            raise NotFound(kb_id)
        files: dict[str, str] = {}
        rows = []
        for doc in sorted(await store.documents(kb_id), key=lambda d: d.name):
            kept = ""
            if doc.kind in ("md", "txt"):
                try:
                    files[f"docs/{doc.name}"] = read_text(Path(doc.path))
                    kept = f"[docs/{doc.name}](docs/{doc.name})"
                except (OSError, ExtractError):
                    log.warning("could not read document %s", doc.document_id, exc_info=True)
            dates = (
                f"{doc.valid_from or ''}～{doc.valid_until or ''}"
                if doc.valid_from or doc.valid_until
                else "长期有效"
            )
            rows.append(
                f"| {doc.name} | {doc.kind} | {doc.status} | {kept or '只列在清单里'}"
                f" | v{doc.version} | {doc.owner or ''} | {dates} |"
            )
        readme = [
            f"# {kb.name}",
            "",
            kb.description or "",
            "",
            f"- 向量模型：{kb.embedding_model}",
            f"- 切块：{kb.chunk_size} 个字符，重叠 {kb.chunk_overlap}",
            "",
            "| 文档 | 类型 | 状态 | 原文 | 版本 | 负责人 | 有效期 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            *rows,
            "",
        ]
        files["README.md"] = "\n".join(readme)
        regression = await store.get_regression(kb_id)
        if regression and regression.cases:
            files["regression.yaml"] = yaml.safe_dump(
                {
                    "retrieval": regression.retrieval,
                    "top_k": regression.top_k,
                    "min_score": regression.min_score,
                    "cases": regression.cases,
                },
                allow_unicode=True,
                sort_keys=False,
            )
        return files


def _eval_yaml(found: dict[str, Any]) -> str:
    data = {
        "app": found["app"],
        "threshold": found["threshold"],
        "judge_model": found["judge_model"],
        "cases": found["cases"],
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
