"""An app's manual: what it is made of, in words people read (``GET /apps/{app}/manual``).

From one configuration of an app (a version, its draft, or the one a run used):
the steps in flow order, each with its note and what it uses, and per resource
(skill, knowledge base, data source, model, outside system) the steps that use
it, plus where people decide. Deleted skill versions and knowledge bases are
still listed, marked "已删除"; no secret's value appears. The contract is
docs/api/data-nodes.md.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aep.config import Settings
from aep.datasources import DataSourceStore
from aep.knowledge.store import KnowledgeStore
from aep.runtime.appconfig import AppOptions
from aep.runtime.flow import refs
from aep.runtime.flow.nodes import TYPES
from aep.skills import SkillStore

DELETED = "已删除"
KIND_LABELS = {"tool": "工具调用", "model": "模型生成", "human": "人工确认", "code": "平台程序"}
DEPLOY_MODES = {
    "job": "notebook Job（已有集群）",
    "pipeline": "Lakeflow 管道",
    "cluster": "只在集群上试跑，不写表",
}


@dataclass(frozen=True)
class Lookups:
    """Where the manual reads names and descriptions of what an app uses."""

    skills: SkillStore
    knowledge: KnowledgeStore
    datasources: DataSourceStore | None
    settings: Settings
    default_model: str


def databricks_detail(s: Settings) -> str:
    """The workspace as the platform uses it, without its token."""
    if not (s.databricks_host and s.databricks_token):
        return "未配置（AEP_DATABRICKS_HOST、AEP_DATABRICKS_TOKEN）"
    host = urlparse(s.databricks_host).hostname or s.databricks_host
    target = (
        f"{s.databricks_catalog}.{s.databricks_schema}"
        if s.databricks_catalog and s.databricks_schema
        else "bundle 默认的 catalog 和 schema"
    )
    parts = [
        f"工作区 {host}",
        f"源表 catalog {s.databricks_source_catalog or '未设置'}",
        f"结果写到 {target}",
        f"运行方式 {DEPLOY_MODES[s.databricks_deploy_mode]}",
    ]
    if s.databricks_cluster_id:
        parts.append(f"集群 {s.databricks_cluster_id}")
    return "；".join(parts)


class _Manual:
    def __init__(self, lookups: Lookups) -> None:
        self._l = lookups
        self.steps: list[dict[str, Any]] = []
        self.skills: dict[tuple[str, int], dict[str, Any]] = {}
        self.bases: dict[str, dict[str, Any]] = {}
        self.sources: dict[tuple[str | None, str], dict[str, Any]] = {}
        self.models: dict[str, dict[str, Any]] = {}
        self.systems: dict[str, dict[str, Any]] = {}
        self.human: list[dict[str, Any]] = []

    async def skill(self, ref: dict[str, Any], node: dict[str, str], uses: list[str]) -> None:
        key = (ref["name"], ref["version"])
        if key not in self.skills:
            content = await self._l.skills.version(*key)
            self.skills[key] = {
                "name": key[0],
                "version": key[1],
                "description": content.description if content else DELETED,
                "nodes": [],
            }
        self.skills[key]["nodes"].append(node)
        uses.append(f"Skill {key[0]} v{key[1]}")

    async def base(self, kb_id: str, node: dict[str, str], uses: list[str]) -> None:
        if kb_id not in self.bases:
            found = await self._l.knowledge.get(kb_id)
            documents = await self._l.knowledge.documents(kb_id) if found else []
            self.bases[kb_id] = {
                "kb_id": kb_id,
                "name": found.name if found else kb_id,
                "description": found.description if found else DELETED,
                "document_count": len(documents),
                "documents": [
                    {"document_id": d.document_id, "filename": d.name, "status": d.status}
                    for d in sorted(documents, key=lambda d: d.name)
                ],
                "nodes": [],
            }
        self.bases[kb_id]["nodes"].append(node)
        uses.append(f"知识库 {self.bases[kb_id]['name']}")

    async def source(
        self, value: Any, start: dict[str, Any] | None, node: dict[str, str], uses: list[str]
    ) -> None:
        """A data source setting: an id, or a reference (chosen when the run starts)."""
        if not isinstance(value, str) or not value.strip():
            return
        ref = refs.whole(value)
        source_id: str | None
        kind: str | None
        if ref is not None:
            label = None
            if start is not None and ref[0] == start.get("id"):
                fields = [f for f in start.get("inputs") or [] if isinstance(f, dict)]
                found = next((f for f in fields if f.get("key") == ref[1]), None)
                label = (found.get("label") or ref[1]) if found else None
            source_id, kind = None, None
            name = f"运行时选择（开始节点的「{label}」）" if label else f"运行时选择（{value}）"
        else:
            found = await self._l.datasources.get(value) if self._l.datasources else None
            source_id = value
            name, kind = (found.name, found.kind) if found else (f"{value}（{DELETED}）", None)
        key = (source_id, name)
        if key not in self.sources:
            self.sources[key] = {
                "datasource_id": source_id,
                "name": name,
                "kind": kind,
                "nodes": [],
            }
        if node not in self.sources[key]["nodes"]:
            self.sources[key]["nodes"].append(node)
            uses.append(f"数据源 {name}")

    def model(self, alias: str | None, node: dict[str, str], uses: list[str]) -> None:
        alias = alias or self._l.default_model
        self.models.setdefault(alias, {"alias": alias, "nodes": []})["nodes"].append(node)
        uses.append(f"模型 {alias}")

    def system(self, key: str, node: dict[str, str], uses: list[str]) -> None:
        if key not in self.systems:
            s = self._l.settings
            if key == "databricks":
                name, detail = "Databricks", databricks_detail(s)
            else:
                name = "资产仓库（GitHub）"
                detail = f"仓库 {s.assets_repo}" if s.assets_repo else "未配置（AEP_ASSETS_REPO）"
            self.systems[key] = {"key": key, "name": name, "detail": detail, "nodes": []}
        self.systems[key]["nodes"].append(node)
        uses.append(self.systems[key]["name"])

    def out(self, head: dict[str, Any]) -> dict[str, Any]:
        return head | {
            "steps": self.steps,
            "skills": list(self.skills.values()),
            "knowledge_bases": list(self.bases.values()),
            "datasources": list(self.sources.values()),
            "models": list(self.models.values()),
            "systems": list(self.systems.values()),
            "human": self.human,
        }


def flow_order(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Nodes breadth first from the start node, edges in the order they are written;
    an edge back to a node already listed is not followed again. Nodes not reached
    (a draft can be unfinished) come last."""
    nodes = [n for n in (config.get("graph") or {}).get("nodes") or [] if isinstance(n, dict)]
    by_id = {n.get("id"): n for n in nodes}
    edges = [e for e in (config.get("graph") or {}).get("edges") or [] if isinstance(e, dict)]
    queue = [n["id"] for n in nodes if n.get("type") == "start"][:1]
    seen: list[str] = []
    while queue:
        current = queue.pop(0)
        if current in seen or current not in by_id:
            continue
        seen.append(current)
        queue += [e.get("to") for e in edges if e.get("from") == current]
    return [by_id[i] for i in seen] + [n for n in nodes if n.get("id") not in seen]


async def flow_manual(
    head: dict[str, Any], config: dict[str, Any], lookups: Lookups
) -> dict[str, Any]:
    manual = _Manual(lookups)
    ordered = flow_order(config)
    start = next((n for n in ordered if n.get("type") == "start"), None)
    for node in ordered:
        spec = TYPES.get(node.get("type"))  # type: ignore[arg-type]
        ref = {"id": node.get("id"), "title": node.get("title") or node.get("id")}
        uses: list[str] = []
        for skill in node.get("skills") or []:
            if isinstance(skill, dict) and "name" in skill and "version" in skill:
                await manual.skill(skill, ref, uses)
        if spec is not None and spec.kind == "model":
            manual.model(node.get("model"), ref, uses)
        if node.get("type") == "knowledge":
            bases = node.get("knowledge_bases") or []
        elif node.get("type") == "agent":
            bases = (node.get("knowledge") or {}).get("knowledge_bases") or []
        else:
            bases = []
        for kb_id in bases:
            if isinstance(kb_id, str) and kb_id:
                await manual.base(kb_id, ref, uses)
        if node.get("type") == "agent":
            for entry in node.get("tools") or []:
                if isinstance(entry, dict):
                    await manual.source(entry.get("datasource"), start, ref, uses)
        elif "datasource" in node:
            await manual.source(node.get("datasource"), start, ref, uses)
        if str(node.get("type")).startswith("databricks_"):
            manual.system("databricks", ref, uses)
        if node.get("type") == "human":
            manual.human.append(ref | {"prompt": node.get("prompt") or ""})
        note = node.get("note")
        manual.steps.append(
            ref
            | {
                "type": node.get("type"),
                "type_label": spec.label if spec else str(node.get("type")),
                "kind": spec.kind if spec else "code",
                "note": note if isinstance(note, str) and note.strip() else None,
                "uses": uses,
            }
        )
    return manual.out(head)


async def code_manual(
    head: dict[str, Any],
    nodes: list[dict[str, Any]],
    config: dict[str, Any],
    options: AppOptions,
    lookups: Lookups,
) -> dict[str, Any]:
    """A code app's manual from its drawn nodes (``aep.runtime.design.shape``, in the
    order the code adds them) and its configuration (``aep.runtime.appconfig``)."""
    manual = _Manual(lookups)
    stored = config.get("nodes") or {}
    for item in nodes:
        design = item.get("design")
        if item["id"].startswith("__") or design is None:
            continue
        ref = {"id": item["id"], "title": item["title"]}
        uses: list[str] = []
        opts = options.get(item["id"])
        value = {**opts.default(), **(stored.get(item["id"]) or {})} if opts else {}
        for skill in value.get("skills") or []:
            await manual.skill(skill, ref, uses)
        if item["kind"] == "model":
            manual.model(value.get("model"), ref, uses)
        params = {**(opts.default()["params"] if opts else {}), **(value.get("params") or {})}
        for param in opts.params if opts else ():
            if param.type == "knowledge_bases":
                for kb_id in params.get(param.key) or []:
                    await manual.base(kb_id, ref, uses)
        tools = " ".join(design.get("tools") or [])
        if "数据源" in tools:
            key = (None, "运行时选择（运行时填写的数据源）")
            entry = manual.sources.setdefault(
                key, {"datasource_id": None, "name": key[1], "kind": None, "nodes": []}
            )
            entry["nodes"].append(ref)
            uses.append(f"数据源 {key[1]}")
        if "Databricks" in tools:
            manual.system("databricks", ref, uses)
        if "资产仓库" in tools:
            manual.system("assets", ref, uses)
        if item["kind"] == "human":
            manual.human.append(ref | {"prompt": design.get("summary") or ""})
        manual.steps.append(
            ref
            | {
                "type": None,
                "type_label": KIND_LABELS.get(item["kind"], ""),
                "kind": item["kind"],
                "note": design.get("summary"),
                "uses": uses,
            }
        )
    return manual.out(head)
