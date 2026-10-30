"""Flow apps as files: one version of an app exported to YAML, and imported back.

The file holds the configuration as it is (secrets by name only, never their
values) and what it needs from the platform it goes to (``requires``), so it can
be kept in the assets repository next to skills and eval sets.
"""

from typing import Any, Literal

import yaml

from aep.datasources import canon_id
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow import refs

FORMAT = "aep-app/1"
MAX_BYTES = 1024 * 1024
# Node types with a ``datasource`` setting (agents name theirs per tool).
DATASOURCE_NODES = ("tool", "etl_profile", "databricks_ingest")


class BadFile(ValueError):
    """Not a flow app file this platform reads; the message says why."""


def datasource_ids(config: dict[str, Any]) -> list[str]:
    """The data sources the app names directly (not through a reference)."""
    found: list[str] = []
    for node in flowgraph.nodes(config):
        values = [node.get("datasource")] if node["type"] in DATASOURCE_NODES else []
        if node["type"] == "agent":
            values = [entry.get("datasource") for entry in node.get("tools") or []]
        for value in values:
            if isinstance(value, str) and value and "{{" not in value and value not in found:
                found.append(value)
    return found


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def datasource_uses(config: dict[str, Any]) -> dict[str, Literal["literal", "default_input"]]:
    """The data sources the app uses, by id (``canon_id``), and how: ``literal`` when a
    setting names it (directly or as a constant, ``{{env.x}}``), ``default_input`` when
    a start input has it as default and is a datasource input or wholly referenced by
    such a setting (docs/api/datasources.md, 使用情况). Reads unchecked configurations
    without failing."""
    config = _obj(config)
    nodes = _items(_obj(config.get("graph")).get("nodes"))
    start = next((n for n in nodes if n.get("type") == "start"), {})
    consts = {v.get("key"): v.get("value") for v in _items(_obj(config.get("env")).get("vars"))}
    settings: list[Any] = []
    for node in nodes:
        if node.get("type") in DATASOURCE_NODES:
            settings.append(node.get("datasource"))
        elif node.get("type") == "agent":
            settings += [entry.get("datasource") for entry in _items(node.get("tools"))]
    found: dict[str, Literal["literal", "default_input"]] = {}
    referenced: set[str] = set()
    for value in settings:
        if not isinstance(value, str):
            continue
        ref = refs.whole(value)
        if ref is None:
            if value.strip() and "{{" not in value:
                found[canon_id(value)] = "literal"
        elif ref[0] == refs.ENV:
            const = consts.get(ref[1])
            if isinstance(const, str) and const.strip():
                found[canon_id(const)] = "literal"
        elif ref[0] == start.get("id"):
            referenced.add(ref[1])
    for f in _items(start.get("inputs")):
        default = f.get("default")
        if f.get("type") == "datasource" or f.get("key") in referenced:
            if isinstance(default, str) and default.strip():
                found.setdefault(canon_id(default), "default_input")
    return found


def knowledge_ids(config: dict[str, Any]) -> list[str]:
    """All bases named by knowledge and agent nodes, in first-use order."""
    found: list[str] = []
    for node in flowgraph.nodes(config):
        settings = node if node["type"] == "knowledge" else node.get("knowledge") or {}
        if node["type"] in ("knowledge", "agent"):
            for kb_id in settings.get("knowledge_bases") or []:
                if kb_id not in found:
                    found.append(kb_id)
    return found


def dump(
    title: str,
    summary: str,
    app: str,
    version: int,
    config: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    knowledge_bases: dict[str, dict[str, Any]],
) -> str:
    """The YAML text; ``sources`` and ``knowledge_bases`` describe required resources by id."""
    data = {
        "format": FORMAT,
        "title": title,
        "summary": summary,
        "exported_from": {"app": app, "version": version},
        "config": config,
        "requires": {
            "skills": [{"name": n, "version": v} for n, v in flowgraph.skill_refs(config)],
            "datasources": [
                sources.get(d, {"datasource_id": d, "name": None, "kind": None})
                for d in datasource_ids(config)
            ],
            "knowledge_bases": [
                knowledge_bases.get(kb_id, {"kb_id": kb_id, "name": None})
                for kb_id in knowledge_ids(config)
            ],
            "secrets": list((config.get("env") or {}).get("secrets") or []),
        },
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def load(text: str) -> dict[str, Any]:
    """The file's contents: ``title``, ``summary``, ``config`` and ``requires``."""
    if len(text.encode()) > MAX_BYTES:
        raise BadFile("文件不能超过 1 MB")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BadFile(f"不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise BadFile("文件内容必须是一个对象")
    if data.get("format") != FORMAT:
        raise BadFile(f"不认识的文件格式 {data.get('format')!r}，这里只读 {FORMAT}")
    if not isinstance(data.get("config"), dict):
        raise BadFile("文件里没有 config")
    return {
        "title": str(data.get("title") or ""),
        "summary": str(data.get("summary") or ""),
        "config": data["config"],
        "requires": data.get("requires") if isinstance(data.get("requires"), dict) else {},
    }
