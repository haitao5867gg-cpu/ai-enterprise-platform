"""How an app is built, in words a business user can follow (the run's flow view).

An app module may define ``DESIGN`` (an ``AppDesign``: what kind of app it is,
where its knowledge comes from, which skills it uses). Each node can carry a
``NodeDesign``: ``graph.add_node(name, fn, metadata=NodeDesign(...).metadata())``.
``flow`` puts them together with the graph's shape and a run's history for
``GET /runs/{id}/flow``; the descriptions live next to the code they describe, so
the view changes when the graph does.
"""

import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

# tool: calls a system outside the platform (a connector, Databricks, GitHub);
# model: a model writes something; human: the run waits for a person;
# code: the platform's own code, no model.
NodeKind = Literal["tool", "model", "human", "code"]

# State keys left out of a step's input: the conversation is in the run itself.
HIDDEN_INPUT = {"messages"}


@dataclass(frozen=True)
class AppDesign:
    title: str
    summary: str
    # How the app decides what happens next (a fixed workflow, or an agent).
    pattern: str
    knowledge: str
    skills: str


@dataclass(frozen=True)
class NodeDesign:
    title: str
    kind: NodeKind
    summary: str
    inputs: str = ""
    outputs: str = ""
    tools: list[str] = field(default_factory=list)
    # The step's key in ``input.models`` (model nodes whose model the run can choose);
    # the model is that alias, or the default one.
    model_step: str | None = None
    prompt: str | None = None
    knowledge: str | None = None
    checks: str | None = None
    retry: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {"design": asdict(self)}


def app_design(build: Any) -> AppDesign | None:
    """The ``DESIGN`` of the module that defines an app's builder, if it has one."""
    design = getattr(sys.modules.get(getattr(build, "__module__", ""), None), "DESIGN", None)
    return design if isinstance(design, AppDesign) else None


def _node(node_id: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
    design = (metadata or {}).get("design")
    if node_id == "__start__":
        return {"id": node_id, "title": "开始", "kind": "terminal", "design": None}
    if node_id == "__end__":
        return {"id": node_id, "title": "结束", "kind": "terminal", "design": None}
    return {
        "id": node_id,
        "title": design["title"] if design else node_id,
        "kind": design["kind"] if design else "code",
        "design": design,
    }


def _status(task: Any) -> str:
    if task.error:
        return "error"
    if task.result is not None:
        return "done"
    return "waiting" if task.interrupts else "running"


async def flow(
    graph: CompiledStateGraph,
    config: dict[str, Any],
    run: dict[str, Any],
    design: AppDesign | None,
    default_model: str,
) -> dict[str, Any]:
    """The graph's nodes and edges, and every step this run took, oldest first.

    ``run`` has the run's ``status`` and ``input``. A step's ``input`` is the state
    the node saw (without the conversation) and ``output`` what it wrote; a step
    still running, or waiting for a person, has no output yet.
    """
    shape = graph.get_graph()
    models = run["input"].get("models") or {}
    nodes = []
    for node_id, node in shape.nodes.items():
        item = _node(node_id, node.metadata)
        if item["kind"] == "model":
            step = item["design"]["model_step"]
            item["model"] = (models.get(step) if step else None) or default_model
        nodes.append(item)
    edges = [
        {"source": e.source, "target": e.target, "conditional": e.conditional} for e in shape.edges
    ]

    snapshots: list[StateSnapshot] = [s async for s in graph.aget_state_history(config)]
    steps = []
    for snapshot in reversed(snapshots):
        values = {k: v for k, v in (snapshot.values or {}).items() if k not in HIDDEN_INPUT}
        for task in snapshot.tasks:
            if task.name.startswith("__"):
                continue
            status = _status(task)
            # A task the run is not on any more never finished (the process stopped).
            if status == "running" and run["status"] != "running":
                status = "error" if run["status"] == "error" else "stopped"
            steps.append(
                {
                    "id": task.id,
                    "node": task.name,
                    "status": status,
                    "started_at": snapshot.created_at,
                    "input": values,
                    "output": task.result,
                    "error": task.error and str(task.error),
                    "interrupt": task.interrupts[0].value if task.interrupts else None,
                }
            )
    current = None
    if run["status"] != "done" and steps and steps[-1]["status"] != "done":
        current = steps[-1]["node"]
    return {
        "design": asdict(design) if design else None,
        "nodes": nodes,
        "edges": edges,
        "steps": steps,
        "current": current,
    }
