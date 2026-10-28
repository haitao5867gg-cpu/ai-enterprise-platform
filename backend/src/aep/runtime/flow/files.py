"""Flow apps as files: one version of an app exported to YAML, and imported back.

The file holds the configuration as it is (secrets by name only, never their
values) and what it needs from the platform it goes to (``requires``), so it can
be kept in the assets repository next to skills and eval sets.
"""

from typing import Any

import yaml

from aep.runtime.flow import graph as flowgraph

FORMAT = "aep-app/1"
MAX_BYTES = 1024 * 1024


class BadFile(ValueError):
    """Not a flow app file this platform reads; the message says why."""


def datasource_ids(config: dict[str, Any]) -> list[str]:
    """The data sources the app names directly (not through a reference)."""
    found: list[str] = []
    for node in flowgraph.nodes(config):
        values = [node.get("datasource")] if node["type"] == "tool" else []
        if node["type"] == "agent":
            values = [entry.get("datasource") for entry in node.get("tools") or []]
        for value in values:
            if isinstance(value, str) and value and "{{" not in value and value not in found:
                found.append(value)
    return found


def knowledge_ids(config: dict[str, Any]) -> list[str]:
    """The knowledge bases named by retrieval nodes, in first-use order."""
    return list(
        dict.fromkeys(
            kb_id
            for node in flowgraph.nodes(config)
            if node["type"] == "knowledge"
            for kb_id in node.get("knowledge_bases") or []
        )
    )


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
