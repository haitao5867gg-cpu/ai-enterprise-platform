"""Flow apps as files: one version of an app exported to YAML, and imported back.

The file holds the configuration as it is (secrets by name only, never their
values) and what it needs from the platform it goes to (``requires``), so it can
be kept in the assets repository next to skills and eval sets.
"""

from typing import Any, Literal

import yaml

from aep.datasources import canon_id
from aep.runtime import agent_app
from aep.runtime.flow import graph as flowgraph
from aep.runtime.flow import media as flowmedia
from aep.runtime.flow import refs

FORMAT = "aep-app/1"
MAX_BYTES = 1024 * 1024
# Node types with a ``datasource`` setting (agents name theirs per tool).
DATASOURCE_NODES = ("tool", "etl_profile", "databricks_ingest", "data_check", "git_pr")


class BadFile(ValueError):
    """Not a flow app file this platform reads; the message says why."""


def datasource_ids(config: dict[str, Any]) -> list[str]:
    """The data sources the app names directly (not through a reference)."""
    found: list[str] = []
    for node in flowgraph.steps(config):
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


def _steps(config: Any) -> list[dict[str, Any]]:
    """``flowgraph.steps`` of a configuration that may not have been checked."""
    found = _items(_obj(_obj(config).get("graph")).get("nodes"))
    return flowgraph.steps({"graph": {"nodes": found}})


def _datasource_settings(config: dict[str, Any]) -> list[tuple[dict[str, Any], Any]]:
    """Every node's data source setting with the node it is on (an agent has one per
    tools entry, given as that entry)."""
    settings: list[tuple[dict[str, Any], Any]] = []
    for node in _steps(config):
        if node.get("type") in DATASOURCE_NODES:
            settings.append((node, node.get("datasource")))
        elif node.get("type") == "agent":
            settings += [(entry, entry.get("datasource")) for entry in _items(node.get("tools"))]
    return settings


def _resolve(
    config: dict[str, Any], value: Any
) -> tuple[str, Literal["literal", "default_input"]] | None:
    """The data source one setting names, by id (``canon_id``), and how (see
    ``datasource_uses``); None when it names none this way."""
    if not isinstance(value, str):
        return None
    config = _obj(config)
    ref = refs.whole(value)
    if ref is None:
        return (canon_id(value), "literal") if value.strip() and "{{" not in value else None
    if ref[0] == refs.ENV:
        consts = {v.get("key"): v.get("value") for v in _items(_obj(config.get("env")).get("vars"))}
        const = consts.get(ref[1])
        return (canon_id(const), "literal") if isinstance(const, str) and const.strip() else None
    nodes = _items(_obj(config.get("graph")).get("nodes"))
    start = next((n for n in nodes if n.get("type") == "start"), {})
    if ref[0] != start.get("id"):
        return None
    for f in _items(start.get("inputs")):
        default = f.get("default")
        if f.get("key") == ref[1] and isinstance(default, str) and default.strip():
            return canon_id(default), "default_input"
    return None


def datasource_uses(config: dict[str, Any]) -> dict[str, Literal["literal", "default_input"]]:
    """The data sources the app uses, by id (``canon_id``), and how: ``literal`` when a
    setting names it (directly or as a constant, ``{{env.x}}``), ``default_input`` when
    a start input has it as default and is a datasource input or wholly referenced by
    such a setting (docs/api/datasources.md, 使用情况). Reads unchecked configurations
    without failing."""
    config = _obj(config)
    nodes = _items(_obj(config.get("graph")).get("nodes"))
    start = next((n for n in nodes if n.get("type") == "start"), {})
    found: dict[str, Literal["literal", "default_input"]] = {}
    referenced: set[str] = set()
    for _, value in _datasource_settings(config):
        if not isinstance(value, str):
            continue
        ref = refs.whole(value)
        if ref is None or ref[0] == refs.ENV:
            resolved = _resolve(config, value)
            if resolved is not None:
                found[resolved[0]] = "literal"
        elif ref[0] == start.get("id"):
            referenced.add(ref[1])
    for f in _items(start.get("inputs")):
        default = f.get("default")
        if f.get("type") == "datasource" or f.get("key") in referenced:
            if isinstance(default, str) and default.strip():
                found.setdefault(canon_id(default), "default_input")
    return found


def datasource_tools(config: dict[str, Any]) -> dict[str, list[str]]:
    """Per data source the app uses (``datasource_uses``), the tools its tool nodes and
    agents name, in first-use order. An agent entry with all of a source's tools
    (``tools: null``) names none: it uses whatever the connector has
    (docs/api/runtime.md, 运行要求清单)."""
    found: dict[str, list[str]] = {source: [] for source in datasource_uses(config)}
    for setting, value in _datasource_settings(config):
        resolved = _resolve(config, value)
        if resolved is None or resolved[0] not in found:
            continue
        if "type" not in setting:  # an agent's tools entry
            named = setting.get("tools")
        else:
            named = [setting.get("tool")] if setting["type"] == "tool" else []
        for tool in named if isinstance(named, list) else []:
            if isinstance(tool, str) and tool.strip() and "{{" not in tool:
                if tool not in found[resolved[0]]:
                    found[resolved[0]].append(tool)
    return found


def unresolved_datasources(config: dict[str, Any]) -> bool:
    """Whether some data source setting names none ``datasource_uses`` can tell before the
    run: a reference to another node's output, an input without a default, or a constant
    that is not set."""
    for _, value in _datasource_settings(config):
        if isinstance(value, str) and value.strip() and _resolve(config, value) is None:
            return True
    return False


# Node types that call a model; a node that names none uses the default chat model.
MODEL_NODES = ("llm", "classifier", "agent", "llm_review")


def model_refs(config: dict[str, Any]) -> tuple[list[str], bool]:
    """The model aliases the app's nodes name, in first-use order, and whether some node
    that calls a model names none (it then uses the default chat model)."""
    aliases: list[str] = []
    default = False
    for node in flowgraph.steps(config):
        if node["type"] == "transcribe":  # always names its model
            model = node.get("model")
            if isinstance(model, str) and model.strip() and model not in aliases:
                aliases.append(model)
            continue
        if node["type"] not in MODEL_NODES:
            continue
        model = node.get("model")
        if isinstance(model, str) and model.strip():
            if model not in aliases:
                aliases.append(model)
        else:
            default = True
    return aliases, default


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
    kind: str = "flow",
    libraries: dict[str, str] | None = None,
) -> str:
    """The YAML text; ``sources``, ``knowledge_bases`` and ``libraries`` (names of media
    libraries) describe required resources by id.
    An agent app's file says ``kind: agent`` and holds its own configuration
    (docs/api/agent-apps.md, "新建、模板、导出导入")."""
    agent = kind == agent_app.KIND
    data = {
        "format": FORMAT,
        **({"kind": agent_app.KIND} if agent else {}),
        "title": title,
        "summary": summary,
        "exported_from": {"app": app, "version": version},
        "config": config,
        "requires": requires(config, sources, knowledge_bases, kind, libraries),
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def requires(
    config: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    knowledge_bases: dict[str, dict[str, Any]],
    kind: str = "flow",
    libraries: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What a configuration needs from the platform it runs on: the file's ``requires``,
    and the derived part of a package's requirements list (docs/api/runtime.md).
    ``sources`` and ``knowledge_bases`` describe resources by id, ``libraries`` names
    media libraries by id."""
    agent = kind == agent_app.KIND
    shape = agent_app.as_flow(config) if agent else config
    models, _ = model_refs(shape)
    out: dict[str, Any] = {
        "skills": [{"name": n, "version": v} for n, v in flowgraph.skill_refs(shape)],
        "models": [{"alias": alias} for alias in models],
        "datasources": [
            {**sources.get(d, {"datasource_id": d, "name": None, "kind": None}), "tools": tools}
            for d, tools in datasource_tools(shape).items()
        ],
        "knowledge_bases": [
            knowledge_bases.get(kb_id, {"kb_id": kb_id, "name": None})
            for kb_id in knowledge_ids(shape)
        ],
        "secrets": list((config.get("env") or {}).get("secrets") or []),
    }
    # Only apps that bind media libraries list them: other files stay as they were.
    if bound := flowmedia.required(config, libraries or {}):
        out["media_libraries"] = bound
    if agent:
        out["apps"] = [a["app"] for a in config["agent"].get("apps") or []]
    return out


def load(text: str) -> dict[str, Any]:
    """The file's contents: ``kind`` (``flow`` when the file names none), ``title``,
    ``summary``, ``config`` and ``requires``."""
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
    kind = data.get("kind", "flow")
    if kind not in ("flow", agent_app.KIND):
        raise BadFile(f"不认识的应用类型 {kind!r}，这里只读编排应用和智能体应用")
    return {
        "kind": kind,
        "title": str(data.get("title") or ""),
        "summary": str(data.get("summary") or ""),
        "config": data["config"],
        "requires": data.get("requires") if isinstance(data.get("requires"), dict) else {},
    }
