"""The ``app`` node: calls another published flow app as one step (docs/api/flow-apps.md,
"app 调用应用"). It starts a run of that app like POST /runs, waits for it and takes the
fields it names from its result; the run service does the starting and waiting
(``Runtime.call_app``)."""

from typing import Any

from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    ID,
    INPUT_ERROR,
    CheckEnv,
    NodeFailure,
    NodeType,
    Outcome,
    Problems,
    Runtime,
)

MAX_OUTPUTS = 20
TIMEOUT = (10, 3600)
DEFAULT_TIMEOUT = 600
# Outputs every app node has, besides the fields it takes.
OWN = ("result", "run_id")


def _outputs(node: dict[str, Any]) -> list[str]:
    value = node.get("outputs")
    return [k for k in value if isinstance(k, str)] if isinstance(value, list) else []


async def check_app(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    name = node.get("app")
    shape = None
    if not isinstance(name, str) or not name.strip():
        p.add(("app",), "要选择被调用的应用")
    elif env.apps is not None and env.strict:
        shape = await env.apps(name)
        if shape is None:
            p.add(("app",), f"没有应用 {name}")
        elif shape.kind != "flow":
            p.add(("app",), f"只能调用编排应用，「{shape.title}」是代码应用")
            shape = None
    given = node.get("input", {})
    if not isinstance(given, dict):
        p.add(("input",), "输入必须是对象")
        given = {}
    if shape is not None:
        inputs = {f["key"]: f for f in shape.inputs}
        for key in given:
            if key not in inputs:
                p.add(("input", key), f"应用「{shape.title}」没有输入 {key}")
        for f in shape.inputs:
            missing = given.get(f["key"]) in (None, "")
            if f.get("required") and f.get("default") is None and missing:
                p.add(("input", f["key"]), f"要填「{f.get('label') or f['key']}」")
    outputs = node.get("outputs", [])
    if not isinstance(outputs, list):
        p.add(("outputs",), "结果字段必须是列表")
        outputs = []
    elif len(outputs) > MAX_OUTPUTS:
        p.add(("outputs",), f"最多取 {MAX_OUTPUTS} 个结果字段")
    seen: set[str] = set()
    for i, key in enumerate(outputs):
        if not isinstance(key, str) or not ID.match(key):
            p.add(("outputs", i), "结果字段要写被调用应用结束节点里的标识")
        elif key in OWN:
            p.add(("outputs", i), f"{key} 是这个节点自带的产出，不用再选")
        elif key in seen:
            p.add(("outputs", i), f"结果字段 {key} 重复")
        elif shape is not None and key not in shape.outputs:
            p.add(("outputs", i), f"应用「{shape.title}」的结果里没有 {key}")
        else:
            seen.add(key)
    timeout = node.get("timeout_seconds", DEFAULT_TIMEOUT)
    low, high = TIMEOUT
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not low <= timeout <= high:
        p.add(("timeout_seconds",), f"最长等待在 {low}–{high} 秒之间")


async def run_app(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    if rt.call_app is None:
        raise NodeFailure(INPUT_ERROR, "这里不能调用别的应用")
    given = refs.resolve(node.get("input") or {}, rt.lookup(state))
    called = await rt.call_app(
        node["app"],
        given,
        state,
        node["id"],
        node.get("timeout_seconds", DEFAULT_TIMEOUT),
    )
    taken = {key: called.result.get(key) for key in _outputs(node)}
    keys = [*taken, *OWN]
    return Outcome(
        {**taken, "result": called.result, "run_id": called.run_id},
        messages=[
            {
                "role": "assistant",
                "content": f"调用「{called.title}」：运行 {called.run_id[:8]} 已完成。",
            }
        ],
        # What another app worked out may come from its data: kept as data values.
        data=keys,
    )


TYPES: dict[str, NodeType] = {
    "app": NodeType(
        "app",
        "调用应用",
        "把另一个已发布的编排应用当成一步：按它的输入发起一次运行，等它结束后取出结果",
        "tool",
        "tool",
        check_app,
        run_app,
        lambda node: None,
        lambda node: [*_outputs(node), *OWN],
        outputs=(
            ("result", "被调用运行的整个结果"),
            ("run_id", "被调用运行的 id"),
        ),
        defaults={"app": "", "input": {}, "outputs": [], "timeout_seconds": DEFAULT_TIMEOUT},
        error_handling=True,
    )
}
