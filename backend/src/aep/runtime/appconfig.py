"""What people may change about an app without touching its code.

An app module may define ``CONFIG``: per node, a ``NodeOptions`` saying whether
people choose the node's model, which skills it uses (and may use), and which
parameters it has. The graph's shape stays in code; an app's configuration is
the value of those options for every node, versioned in
``aep.runtime.versions``. Its defaults are the app's version 1.

A run uses one configuration from start to end. ``RunService`` loads it, with
the text of every skill version it names, and hands it to the graph in the
LangGraph config; a node reads its part with ``node_config(config, "plan")``.
A model node's system prompt is ``NodeConfig.prompt()``: its skills' text, then
``NodeOptions.contract``, the fixed part the platform depends on (the output
format it parses, the rules its checks enforce).
"""

import sys
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from aep.skills import SkillContent, SkillStore, builtin_skills

CONFIG_KEY = "app_config"

ParamType = Literal["integer", "enum", "boolean", "string"]


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    type: ParamType
    default: Any
    help: str = ""
    min: int | None = None
    max: int | None = None
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)
    max_length: int | None = None

    def setting(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label, "type": self.type}
        if self.help:
            out["help"] = self.help
        if self.type == "integer":
            out |= {"min": self.min, "max": self.max}
        if self.type == "enum":
            out["choices"] = [{"value": v, "label": label} for v, label in self.choices]
        if self.type == "string" and self.max_length is not None:
            out["max_length"] = self.max_length
        return out

    def problem(self, value: Any) -> str | None:
        """Why ``value`` is not allowed, or None."""
        if self.type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return f"{self.label}必须是整数"
            if self.min is not None and value < self.min:
                return f"{self.label}不能小于 {self.min}"
            if self.max is not None and value > self.max:
                return f"{self.label}不能大于 {self.max}"
        elif self.type == "enum":
            if value not in [v for v, _ in self.choices]:
                return f"{self.label}只能是 " + "、".join(v for v, _ in self.choices)
        elif self.type == "boolean":
            if not isinstance(value, bool):
                return f"{self.label}必须是 true 或 false"
        elif not isinstance(value, str):
            return f"{self.label}必须是文字"
        elif self.max_length is not None and len(value) > self.max_length:
            return f"{self.label}最多 {self.max_length} 个字符"
        return None


@dataclass(frozen=True)
class NodeOptions:
    """What may be set on one node. ``skills`` None: the node takes no skills; otherwise
    the skills it uses by default (their version 1, the built-in one)."""

    model: bool = False
    skills: tuple[str, ...] | None = None
    contract: str = ""
    params: tuple[Param, ...] = ()

    def settings(self) -> list[dict[str, Any]]:
        out = []
        if self.model:
            out.append({"key": "model", "label": "模型", "type": "model"})
        if self.skills is not None:
            out.append({"key": "skills", "label": "Skill", "type": "skills"})
        return out + [p.setting() for p in self.params]

    def default(self) -> dict[str, Any]:
        return {
            "model": None,
            "skills": [{"name": name, "version": 1} for name in self.skills or ()],
            "params": {p.key: p.default for p in self.params},
        }


AppOptions = dict[str, NodeOptions]


def app_options(build: Any) -> AppOptions:
    """The ``CONFIG`` of the module that defines an app's builder ({} if it has none)."""
    found = getattr(sys.modules.get(getattr(build, "__module__", ""), None), "CONFIG", None)
    return dict(found) if isinstance(found, dict) else {}


def default_config(options: AppOptions) -> dict[str, Any]:
    return {"nodes": {node: o.default() for node, o in options.items()}}


class ConfigError(ValueError):
    """``problems`` are (location, message) pairs, location as in FastAPI's 422 ``loc``."""

    def __init__(self, problems: list[tuple[list[str | int], str]]) -> None:
        super().__init__("；".join(message for _, message in problems))
        self.problems = problems


async def validate_config(
    raw: Any, options: AppOptions, skills: SkillStore, models: list[str] | None
) -> dict[str, Any]:
    """``raw`` checked against the app's options and completed with defaults.

    ``models`` are the aliases the gateway offers; None when it could not be asked,
    and then any alias is taken.
    """
    problems: list[tuple[list[str | int], str]] = []
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, dict):
        raise ConfigError([(["config", "nodes"], "缺少 nodes")])
    for node in nodes:
        if node not in options:
            problems.append((["config", "nodes", node], f"节点 {node} 没有可调设置"))
    out: dict[str, Any] = {}
    for node, opts in options.items():
        loc: list[str | int] = ["config", "nodes", node]
        given = nodes.get(node) or {}
        if not isinstance(given, dict):
            problems.append((loc, "节点设置必须是对象"))
            continue
        value = opts.default()
        model = given.get("model")
        if model is not None:
            if not opts.model:
                problems.append(([*loc, "model"], "这个节点不能选模型"))
            elif not isinstance(model, str) or not model.strip():
                problems.append(([*loc, "model"], "模型别名不能为空"))
            elif models is not None and model not in models:
                problems.append(([*loc, "model"], f"模型网关上没有 {model}"))
            else:
                value["model"] = model
        if "skills" in given:
            chosen = given["skills"]
            if opts.skills is None:
                if chosen:
                    problems.append(([*loc, "skills"], "这个节点不能用 Skill"))
            elif not isinstance(chosen, list):
                problems.append(([*loc, "skills"], "skills 必须是列表"))
            else:
                value["skills"] = []
                seen = set()
                for i, ref in enumerate(chosen):
                    name = ref.get("name") if isinstance(ref, dict) else None
                    version = ref.get("version") if isinstance(ref, dict) else None
                    if not isinstance(name, str) or not isinstance(version, int):
                        problems.append(([*loc, "skills", i], "每项要有 name 和整数 version"))
                    elif name in seen:
                        problems.append(([*loc, "skills", i], f"Skill {name} 重复"))
                    elif await skills.version(name, version) is None:
                        problems.append(
                            ([*loc, "skills", i], f"Skill {name} 没有已发布的版本 {version}")
                        )
                    else:
                        seen.add(name)
                        value["skills"].append({"name": name, "version": version})
        params = given.get("params") or {}
        if not isinstance(params, dict):
            problems.append(([*loc, "params"], "params 必须是对象"))
            params = {}
        known = {p.key: p for p in opts.params}
        for key, v in params.items():
            if key not in known:
                problems.append(([*loc, "params", key], f"没有参数 {key}"))
            elif problem := known[key].problem(v):
                problems.append(([*loc, "params", key], problem))
            else:
                value["params"][key] = v
        out[node] = value
    if problems:
        raise ConfigError(problems)
    return {"nodes": out}


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    version: int
    content: SkillContent


@dataclass(frozen=True)
class NodeConfig:
    """One node's configuration in a run, with its skills' text loaded."""

    model: str | None = None
    skills: tuple[LoadedSkill, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    contract: str = ""

    def prompt(self) -> str:
        parts = [s.content.text() for s in self.skills]
        if self.contract:
            parts.append(self.contract.strip())
        return "\n\n".join(parts)

    def refs(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "version": s.version} for s in self.skills]


async def load(
    config: dict[str, Any], options: AppOptions, skills: SkillStore
) -> dict[str, NodeConfig]:
    """Every node's configuration, skills read from ``skills``. Nodes the stored
    configuration does not have (added to the app since) get their defaults."""
    loaded: dict[str, NodeConfig] = {}
    nodes = config.get("nodes") or {}
    for node, opts in options.items():
        value = {**opts.default(), **(nodes.get(node) or {})}
        params = {**opts.default()["params"], **(value.get("params") or {})}
        found = []
        for ref in value.get("skills") or []:
            content = await skills.version(ref["name"], ref["version"])
            if content is None:
                raise LookupError(f"Skill {ref['name']} 没有版本 {ref['version']}")
            found.append(LoadedSkill(ref["name"], ref["version"], content))
        loaded[node] = NodeConfig(value.get("model"), tuple(found), params, opts.contract)
    return loaded


def builtin(options: NodeOptions) -> NodeConfig:
    """A node's defaults with the built-in skills, for a graph run without a config."""
    skills = builtin_skills()
    return NodeConfig(
        None,
        tuple(LoadedSkill(name, 1, skills[name]) for name in options.skills or ()),
        options.default()["params"],
        options.contract,
    )


def node_config(config: RunnableConfig, node: str, options: AppOptions) -> NodeConfig:
    """The node's configuration in this run (its defaults if the run has none)."""
    found = (config.get("configurable") or {}).get(CONFIG_KEY) or {}
    return found.get(node) or builtin(options[node])
