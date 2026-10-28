"""Turning a flow app's configuration into a LangGraph graph, and into a diagram.

Every node writes its outputs into the shared variable pool (``vars``), counts
how often it ran (``visits``) and, if it has branches, which one it took
(``branch``); conditional edges follow that. The end node writes the run's
``result``. ``on_error`` is applied here, around the node type's ``run``.
"""

import asyncio
import operator
from collections import defaultdict
from typing import Annotated, Any, TypedDict

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from aep.runtime.design import NodeDesign
from aep.runtime.flow.check import DEFAULT_MAX_VISITS, branches
from aep.runtime.flow.nodes import ERROR, TYPES, NodeFailure, Outcome, Runtime

# The branch key of an edge without a branch.
NEXT = "next"
# The most nodes one run may execute.
MAX_STEPS = 200
# How every flow app is built, for the run's flow view and the app's page.
DESIGN = {
    "pattern": "编排应用：按配置里的节点和连线运行，结构可以在页面上修改。",
    "knowledge": "没有接知识库；用到的数据来自节点里选的数据源工具。",
    "skills": "Skill 挂在调用模型、意图分类和智能体节点上，每个 Skill 指定版本。",
}


def _merge(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    return {**(old or {}), **(new or {})}


class FlowState(TypedDict, total=False):
    input: dict[str, Any]
    vars: Annotated[dict[str, dict[str, Any]], _merge]
    visits: Annotated[dict[str, int], _merge]
    branch: Annotated[dict[str, str], _merge]
    messages: Annotated[list[dict[str, str]], operator.add]
    result: dict[str, Any] | None


class FlowError(Exception):
    """A node failed and the run stops; the message names the node."""

    def __init__(self, node: dict[str, Any], message: str) -> None:
        super().__init__(f"节点「{node.get('title', node['id'])}」({node['id']})：{message}")


def nodes(config: dict[str, Any]) -> list[dict[str, Any]]:
    return list(config["graph"]["nodes"])


def start_node(config: dict[str, Any]) -> dict[str, Any]:
    return next(n for n in nodes(config) if n["type"] == "start")


def inputs(config: dict[str, Any]) -> list[dict[str, Any]]:
    """What a run of the app asks for: its start node's inputs."""
    return list(start_node(config).get("inputs") or [])


def skill_refs(config: dict[str, Any]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in nodes(config):
        for ref in node.get("skills") or []:
            key = (ref["name"], ref["version"])
            if key not in found:
                found.append(key)
    return found


async def execute(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    """Run the node, retrying and handling failure as its ``on_error`` says."""
    spec = TYPES[node["type"]]
    if not spec.error_handling:
        try:
            return await spec.run(node, state, rt)
        except NodeFailure as exc:
            raise FlowError(node, exc.message) from exc
    on_error = node.get("on_error") or {}
    retry = on_error.get("retry") or {}
    attempts = 1 + int(retry.get("max", 0))
    failure = NodeFailure("model_error", "")
    for attempt in range(attempts):
        try:
            return await spec.run(node, state, rt)
        except NodeFailure as exc:
            failure = exc
        if attempt + 1 < attempts:
            await asyncio.sleep(float(retry.get("interval_seconds", 0)))
    then = on_error.get("then", "stop")
    if then == "stop":
        raise FlowError(node, failure.message)
    error = {"error_type": failure.type, "error_message": failure.message}
    if then == "default":
        produced = {key: None for key in spec.produces(node)}
        return Outcome({**produced, **(on_error.get("default") or {}), **error})
    return Outcome(error, ERROR)


def design(node: dict[str, Any], rt: Runtime | None = None) -> NodeDesign:
    spec = TYPES[node["type"]]
    prompt = None
    if spec.kind == "model":
        prompt = rt.system(node, node.get("system") or "") if rt else node.get("system")
    return NodeDesign(
        title=node.get("title") or node["id"],
        kind=spec.kind,
        summary=f"{spec.label}：{spec.description}",
        outputs="、".join(spec.produces(node)),
        prompt=prompt,
        retry=_retry_text(node) if spec.error_handling else None,
        type=node["type"],
    )


def _retry_text(node: dict[str, Any]) -> str | None:
    on_error = node.get("on_error") or {}
    times = (on_error.get("retry") or {}).get("max", 0)
    then = {"stop": "停止运行", "default": "用默认值继续", "branch": "走“出错”分支"}[
        on_error.get("then", "stop")
    ]
    return f"出错时重试 {times} 次，仍失败就{then}" if times else f"出错时{then}"


def _node_fn(node: dict[str, Any], rt: Runtime) -> Any:
    node_id = node["id"]
    limit = node.get("max_visits", DEFAULT_MAX_VISITS)

    async def run(state: FlowState) -> dict[str, Any]:
        visits = (state.get("visits") or {}).get(node_id, 0)
        if visits >= limit:
            raise FlowError(node, f"已经运行了 {visits} 次，达到上限 {limit} 次")
        try:
            outcome = await execute(node, dict(state), rt)
        except (GraphBubbleUp, FlowError):
            raise
        except Exception as exc:
            raise FlowError(node, str(exc) or type(exc).__name__) from exc
        update: dict[str, Any] = {
            "vars": {node_id: outcome.outputs},
            "visits": {node_id: visits + 1},
            "branch": {node_id: outcome.branch or NEXT},
        }
        if outcome.messages:
            update["messages"] = outcome.messages
        if outcome.result is not None:
            update["result"] = outcome.result
        return update

    return run


def build(config: dict[str, Any], rt: Runtime) -> StateGraph:
    """The app's graph; ``config`` passed ``check_config``."""
    graph = StateGraph(FlowState)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in config["graph"]["edges"]:
        out[edge["from"]].append(edge)
    for node in nodes(config):
        graph.add_node(node["id"], _node_fn(node, rt), metadata=design(node, rt).metadata())
    graph.add_edge(START, start_node(config)["id"])
    for node in nodes(config):
        node_id = node["id"]
        if node["type"] == "end":
            graph.add_edge(node_id, END)
            continue
        edges = out[node_id]
        if branches(node) == [None]:
            graph.add_edge(node_id, edges[0]["to"])
            continue
        path = {edge.get("branch") or NEXT: edge["to"] for edge in edges}
        graph.add_conditional_edges(node_id, _router(node_id), path)  # type: ignore[arg-type]
    return graph


def _router(node_id: str) -> Any:
    def route(state: FlowState) -> str:
        return (state.get("branch") or {}).get(node_id, NEXT)

    return route


def shape(config: dict[str, Any], rt: Runtime | None = None) -> tuple[list[dict], list[dict]]:
    """The app's nodes and edges as the flow view draws them (``__start__`` and
    ``__end__`` included); branch edges carry the branch's ``label``."""
    drawn = [{"id": "__start__", "title": "开始", "kind": "terminal", "type": None, "design": None}]
    for node in nodes(config):
        d = design(node, rt).metadata()["design"]
        drawn.append(
            {
                "id": node["id"],
                "title": d["title"],
                "kind": d["kind"],
                "type": node["type"],
                "design": d,
            }
        )
    drawn.append(
        {"id": "__end__", "title": "结束", "kind": "terminal", "type": None, "design": None}
    )
    by_id = {n["id"]: n for n in nodes(config)}
    edges = [
        {
            "source": "__start__",
            "target": start_node(config)["id"],
            "conditional": False,
            "label": None,
        }
    ]
    for edge in config["graph"]["edges"]:
        branch = edge.get("branch")
        source = by_id[edge["from"]]
        labels = TYPES[source["type"]].branch_labels(source) | {ERROR: "出错"}
        edges.append(
            {
                "source": edge["from"],
                "target": edge["to"],
                "conditional": branch is not None,
                "label": labels.get(branch, branch) if branch else None,
            }
        )
    for node in nodes(config):
        if node["type"] == "end":
            edges.append(
                {"source": node["id"], "target": "__end__", "conditional": False, "label": None}
            )
    return drawn, edges
