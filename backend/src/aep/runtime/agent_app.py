"""Agent apps: one agent set up on a form (docs/api/agent-apps.md).

An agent app keeps its own configuration (``AgentAppConfig``: ``agent`` and ``chat``)
and runs as a generated flow graph, start → agent → end, on the flow runtime. So
versions, evals, tracing, budgets, the outbound policy and the run page work as for
any flow app; what the agent node can do is the node's (aep.runtime.flow.nodes).
"""

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from aep.conversations import DEFAULT_HISTORY_TURNS, MAX_HISTORY_TURNS, MESSAGE
from aep.runtime.appconfig import ConfigError
from aep.runtime.flow import refs

KIND = "agent"
SCHEMA_VERSION = 1
MAX_INSTRUCTIONS = 20000
MAX_OPENING = 500
MAX_SUGGESTIONS = 5
MAX_SUGGESTION = 100
DEFAULT_STEPS = 8
# The generated graph's node ids and titles.
START, AGENT, END = "start", "agent", "end"
TITLES = {START: "收到消息", AGENT: "智能体", END: "回答"}
MESSAGE_INPUT = {"key": MESSAGE, "label": "消息", "type": "textarea", "required": True}
# What the agent's run gives back (``result``).
RESULT = ("reply", "steps", "hits", "grounded")
SOURCES = {"reply": "text", "steps": "steps", "hits": "hits", "grounded": "grounded"}
AGENT_KEYS = (
    "instructions",
    "model",
    "skills",
    "tools",
    "knowledge",
    "apps",
    "grounded_only",
    "max_steps",
    "history_turns",
)


def is_agent(config: Any) -> bool:
    """Whether a stored configuration is an agent app's (it has ``agent``, no graph)."""
    return (
        isinstance(config, dict) and isinstance(config.get("agent"), dict) and "graph" not in config
    )


def config(
    instructions: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    knowledge: dict[str, Any] | None = None,
    opening: str = "",
    suggestions: list[str] | None = None,
    max_steps: int = DEFAULT_STEPS,
) -> dict[str, Any]:
    """An agent app's configuration with the defaults filled in."""
    return {
        "schema_version": SCHEMA_VERSION,
        "env": {"vars": [], "secrets": []},
        "agent": {
            "instructions": instructions,
            "model": None,
            "skills": [],
            "tools": tools or [],
            "knowledge": knowledge,
            "apps": [],
            "grounded_only": True,
            "max_steps": max_steps,
            "history_turns": DEFAULT_HISTORY_TURNS,
        },
        "chat": {"opening": opening, "suggestions": suggestions or []},
    }


def blank() -> dict[str, Any]:
    """A new agent app: instructions only, ready to chat."""
    return config(
        "你是一个乐于助人的助手。回答简洁、准确；不知道的就直说，不要编造。",
        opening="你好，有什么可以帮你？",
    )


def knowledge_template() -> dict[str, Any]:
    return config(
        "你是知识库助手。先在知识库里检索资料，再根据资料回答，并用 [n] 标出处；"
        "资料里没有的就说没有找到，不要编造。",
        knowledge={"knowledge_bases": [], "top_k": 5, "min_score": 0.3},
        opening="你好，我可以根据知识库里的资料回答问题。",
        max_steps=6,
    )


def data_template() -> dict[str, Any]:
    return config(
        "你是数据问答助手。回答数据问题时，先看有哪些表和字段，再查需要的数据；"
        "只用查到的数字回答，并说明用到了哪些表和数据。",
        tools=[{"datasource": "", "tools": None}],
        opening="你好，我可以查数据回答问题。",
    )


def as_flow(config: dict[str, Any]) -> dict[str, Any]:
    """The flow configuration an agent app runs as (docs/api/agent-apps.md, "生成的图")."""
    agent = config.get("agent") or {}
    node = {
        "id": AGENT,
        "type": "agent",
        "title": TITLES[AGENT],
        "model": agent.get("model"),
        "skills": copy.deepcopy(agent.get("skills") or []),
        "task": "{{start.message}}",
        "instructions": agent.get("instructions") or "",
        "tools": copy.deepcopy(agent.get("tools") or []),
        "knowledge": copy.deepcopy(agent.get("knowledge")),
        "apps": copy.deepcopy(agent.get("apps") or []),
        "grounded_only": agent.get("grounded_only", True),
        "max_steps": agent.get("max_steps", DEFAULT_STEPS),
        "history": True,
    }
    return {
        "schema_version": config.get("schema_version", SCHEMA_VERSION),
        "env": copy.deepcopy(config.get("env") or {"vars": [], "secrets": []}),
        "conversation": {
            "enabled": True,
            "history_turns": agent.get("history_turns", DEFAULT_HISTORY_TURNS),
            "reply": "reply",
        },
        "graph": {
            "nodes": [
                {
                    "id": START,
                    "type": "start",
                    "title": TITLES[START],
                    "inputs": [dict(MESSAGE_INPUT)],
                },
                node,
                {
                    "id": END,
                    "type": "end",
                    "title": TITLES[END],
                    "outputs": [
                        {"key": key, "from": [f"{{{{{AGENT}.{SOURCES[key]}}}}}"]} for key in RESULT
                    ],
                },
            ],
            "edges": [{"from": START, "to": AGENT}, {"from": AGENT, "to": END}],
        },
    }


Loc = tuple[str | int, ...]
CheckFlow = Callable[[Any], Awaitable[dict[str, Any]]]


def _where(loc: list[str | int]) -> list[str | int]:
    """Where a problem of the generated graph is in the agent app's configuration."""
    if loc[:4] == ["config", "graph", "nodes", AGENT]:
        return ["config", "agent", *loc[4:]]
    if loc[:3] == ["config", "conversation", "history_turns"]:
        return ["config", "agent", "history_turns"]
    return loc


async def check(raw: Any, check_flow: CheckFlow) -> dict[str, Any]:
    """An agent app's configuration as it is kept; raises ``ConfigError`` with each
    problem where it is in the configuration (``check_flow`` checks the generated graph)."""
    problems: list[tuple[list[str | int], str]] = []

    def add(loc: Loc, message: str) -> None:
        problems.append((["config", *loc], message))

    if not isinstance(raw, dict):
        raise ConfigError([(["config"], "配置必须是对象")])
    agent = raw.get("agent")
    if not isinstance(agent, dict):
        raise ConfigError([(["config", "agent"], "智能体应用的配置要有 agent")])
    unknown = sorted(set(agent) - set(AGENT_KEYS))
    for key in unknown:
        add(("agent", key), f"智能体没有 {key} 这一项")
    instructions = agent.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        add(("agent", "instructions"), "指令不能为空")
    elif len(instructions) > MAX_INSTRUCTIONS:
        add(("agent", "instructions"), f"指令最多 {MAX_INSTRUCTIONS} 个字符")
    tools = agent.get("tools", [])
    if isinstance(tools, list):
        for i, entry in enumerate(tools):
            source = entry.get("datasource") if isinstance(entry, dict) else None
            if isinstance(source, str) and refs.find(source):
                add(("agent", "tools", i, "datasource"), "智能体应用里要直接选数据源，不能写引用")
    turns = agent.get("history_turns", DEFAULT_HISTORY_TURNS)
    if not isinstance(turns, int) or isinstance(turns, bool) or not 0 <= turns <= MAX_HISTORY_TURNS:
        add(("agent", "history_turns"), f"带的历史轮数要在 0–{MAX_HISTORY_TURNS} 之间")
        turns = DEFAULT_HISTORY_TURNS
    if "grounded_only" in agent and not isinstance(agent["grounded_only"], bool):
        add(("agent", "grounded_only"), "grounded_only 必须是 true 或 false")
    chat = _check_chat(raw.get("chat"), add)
    if problems:
        raise ConfigError(problems)

    shaped = {**raw, "agent": {**agent, "history_turns": turns}}
    try:
        checked = await check_flow(as_flow(shaped))
    except ConfigError as exc:
        raise ConfigError([(_where(list(loc)), message) for loc, message in exc.problems]) from exc
    node = next(n for n in checked["graph"]["nodes"] if n["id"] == AGENT)
    kept = {key: node.get(key) for key in AGENT_KEYS if key != "history_turns"}
    kept["apps"] = node.get("apps") or []
    kept["grounded_only"] = node.get("grounded_only", True)
    kept["max_steps"] = node.get("max_steps", DEFAULT_STEPS)
    kept["history_turns"] = checked["conversation"]["history_turns"]
    return {
        "schema_version": checked["schema_version"],
        "env": checked["env"],
        "agent": kept,
        "chat": chat,
    }


def _check_chat(raw: Any, add: Callable[[Loc, str], None]) -> dict[str, Any]:
    if raw is None:
        return {"opening": "", "suggestions": []}
    if not isinstance(raw, dict):
        add(("chat",), "chat 必须是对象")
        return {"opening": "", "suggestions": []}
    opening = raw.get("opening") or ""
    if not isinstance(opening, str):
        add(("chat", "opening"), "开场白必须是文字")
        opening = ""
    elif len(opening) > MAX_OPENING:
        add(("chat", "opening"), f"开场白最多 {MAX_OPENING} 个字符")
    suggestions = raw.get("suggestions") or []
    if not isinstance(suggestions, list) or len(suggestions) > MAX_SUGGESTIONS:
        add(("chat", "suggestions"), f"推荐问题最多 {MAX_SUGGESTIONS} 个")
        suggestions = []
    for i, text in enumerate(suggestions):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SUGGESTION:
            add(("chat", "suggestions", i), f"每个推荐问题 1–{MAX_SUGGESTION} 个字符")
    return {"opening": opening, "suggestions": list(suggestions)}
