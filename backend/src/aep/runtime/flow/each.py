"""The ``each`` node (逐项处理): does one step (an llm, tool, code, template or transcribe
node's settings, ``do``) for every item of a list and puts out the results in the list's
order (docs/api/each.md). The step reads the item as ``{{item.*}}``."""

import asyncio
import dataclasses
from typing import Any

from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    INPUT_ERROR,
    TYPES,
    CheckEnv,
    NodeFailure,
    NodeType,
    Outcome,
    Problems,
    Runtime,
)

# What a step may be: nodes with one way out whose outputs are values, not a decision.
STEP_TYPES = ("llm", "tool", "code", "template", "transcribe")
MAX_ITEMS = 100
CONCURRENCY = (1, 10)
DEFAULT_CONCURRENCY = 3
ON_ITEM_ERROR = ("stop", "skip")
# The common fields of a node: the each node has them, its step does not.
COMMON = ("id", "title", "on_error", "max_visits", "note", "ui")
OUTPUTS = ("results", "failed", "count")
# What ``{{item.*}}`` always names: the whole item and where it is in the list.
WHOLE, POSITION = "value", "position"


def step(node: dict[str, Any]) -> dict[str, Any] | None:
    """The node's step as a node of its own (its id and title), or None when ``do`` is not
    a step of a known kind."""
    do = node.get("do")
    if not isinstance(do, dict) or do.get("type") not in STEP_TYPES:
        return None
    return {**do, "id": node["id"], "title": node.get("title") or node["id"]}


def step_settings(node: dict[str, Any]) -> dict[str, Any]:
    """The each node's settings as references are read from them: the step's settings taken
    as they are (a code step's ``code``) left out."""
    do = node.get("do")
    spec = TYPES.get(do.get("type")) if isinstance(do, dict) else None  # type: ignore[arg-type]
    if spec is None or not isinstance(do, dict):
        return node
    return {**node, "do": {k: v for k, v in do.items() if k not in spec.verbatim}}


def _choice(node: dict[str, Any]) -> str:
    value = node.get("on_item_error", "stop")
    return value if value in ON_ITEM_ERROR else "stop"


async def check_each(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    items = node.get("items")
    if not isinstance(items, str) or refs.whole(items) is None:
        p.add(("items",), "要插一个变量：对哪个列表逐项处理")
    do = node.get("do")
    if not isinstance(do, dict):
        p.add(("do",), "要设置每一项做什么")
    elif do.get("type") not in STEP_TYPES:
        kinds = "、".join(TYPES[t].label for t in STEP_TYPES)
        p.add(("do", "type"), f"每一项只能做这几种：{kinds}")
    else:
        for key in COMMON:
            if key in do:
                p.add(("do", key), f"每一项做的那一步不写 {key}")
        inner = Problems((*p.prefix, "do"))
        await TYPES[do["type"]].check({**do, "id": node.get("id") or "each"}, inner, env)
        p.found.extend(inner.found)
    concurrency = node.get("concurrency", DEFAULT_CONCURRENCY)
    low, high = CONCURRENCY
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not low <= concurrency <= high
    ):
        p.add(("concurrency",), f"同时处理几项要在 {low}–{high} 之间")
    if node.get("on_item_error", "stop") not in ON_ITEM_ERROR:
        p.add(("on_item_error",), "某一项失败时只能是 stop（停下）或 skip（跳过）")


def _with_item(
    state: dict[str, Any], value: Any, position: int, data: bool, quoted: bool
) -> dict[str, Any]:
    """The run's state as one item's step reads it: ``{{item.*}}`` is that item, a data
    value (or text that may quote one) when the list is."""
    fields = dict(value) if isinstance(value, dict) else {}
    item = {**fields, WHOLE: value, POSITION: position}
    out = {**state, "vars": {**(state.get("vars") or {}), refs.ITEM: item}}
    keys = [k for k in item if k != POSITION]
    if data:
        out["data"] = {**(state.get("data") or {}), refs.ITEM: keys}
    if quoted:
        out["quoted"] = {**(state.get("quoted") or {}), refs.ITEM: keys}
    return out


@dataclasses.dataclass
class _Failed(Exception):
    position: int
    failure: NodeFailure


async def run_each(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    body = step(node)
    if body is None:
        raise NodeFailure(INPUT_ERROR, "没有设置每一项做什么")
    ref = refs.whole(node.get("items") or "")
    items = rt.lookup(state)(*ref) if ref else None
    if not isinstance(items, list):
        raise NodeFailure(INPUT_ERROR, "「items」取到的不是列表")
    if len(items) > MAX_ITEMS:
        raise NodeFailure(INPUT_ERROR, f"最多处理 {MAX_ITEMS} 项，这次有 {len(items)} 项")
    spec = TYPES[body["type"]]
    # Only this node says how far it got: a long-running tool job in a step does not.
    inner = dataclasses.replace(rt, report_progress=None)
    kind = rt.classify(state, node["items"], [refs.ITEM])
    data, quoted = "data" in kind, "quoted" in kind
    retry = (node.get("on_error") or {}).get("retry") or {}
    attempts = 1 + int(retry.get("max", 0))
    interval = float(retry.get("interval_seconds", 0))
    skip = _choice(node) == "skip"
    gate = asyncio.Semaphore(node.get("concurrency", DEFAULT_CONCURRENCY))
    total = len(items)
    results: list[Any] = [None] * total
    outcomes: list[Outcome | None] = [None] * total
    failed: list[dict[str, Any]] = []
    done = 0
    run_id = str(state.get("run_id") or "")

    async def report() -> None:
        if rt.report_progress is None or not run_id or not total:
            return
        message = f"已完成 {done}/{total} 项"
        if failed:
            message += f"，跳过 {len(failed)} 项"
        await rt.report_progress(run_id, node["id"], done / total, message)

    async def one(index: int, value: Any) -> None:
        nonlocal done
        position = index + 1
        async with gate:
            item_state = _with_item(state, value, position, data, quoted)
            failure = NodeFailure("model_error", "")
            for attempt in range(attempts):
                try:
                    outcome = await spec.run(body, item_state, inner)
                except NodeFailure as exc:
                    failure = exc
                else:
                    outcomes[index] = outcome
                    results[index] = outcome.outputs
                    break
                if failure.type == INPUT_ERROR:
                    break
                if attempt + 1 < attempts:
                    await asyncio.sleep(interval)
            if outcomes[index] is None:
                if not skip:
                    raise _Failed(position, failure)
                failed.append(
                    {
                        "position": position,
                        "error_type": failure.type,
                        "error_message": failure.message,
                    }
                )
            done += 1
            await report()

    tasks = [asyncio.ensure_future(one(i, v)) for i, v in enumerate(items)]
    try:
        for finished in asyncio.as_completed(tasks):
            try:
                await finished
            except _Failed as exc:
                raise NodeFailure(
                    exc.failure.type, f"第 {exc.position} 项：{exc.failure.message}"
                ) from exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if total and len(failed) == total:
        first = min(failed, key=lambda f: f["position"])
        raise NodeFailure(
            first["error_type"],
            f"每一项都失败了，第 {first['position']} 项：{first['error_message']}",
        )
    failed.sort(key=lambda f: f["position"])
    made = [o for o in outcomes if o is not None]
    restored: dict[str, str] = {}
    for o in made:
        restored.update(o.restored)
    return Outcome(
        {"results": results, "failed": failed, "count": total},
        data=["results"] if any(o.data for o in made) else [],
        # Why an item failed may quote the data it was given (aep.policy).
        quoted=["failed", *(["results"] if any(o.quoted for o in made) else [])],
        restored=restored,
    )


def _produces(node: dict[str, Any]) -> list[str]:
    return list(OUTPUTS)


EACH_TYPES: dict[str, NodeType] = {
    "each": NodeType(
        "each",
        "逐项处理",
        "对一个列表的每一项做同一步（调模型、调工具、跑代码……），收齐结果再往下走",
        "logic",
        "code",
        check_each,
        run_each,
        lambda node: None,
        _produces,
        outputs=(
            ("results", "每一项的产出，顺序同列表；跳过的项为空"),
            ("failed", "失败后跳过的项：第几项和原因"),
            ("count", "一共几项"),
        ),
        defaults={
            "items": "",
            "do": {"type": "llm", **TYPES["llm"].defaults},
            "concurrency": DEFAULT_CONCURRENCY,
            "on_item_error": "stop",
        },
        error_handling=True,
        own_retry=True,
    )
}

TYPES.update(EACH_TYPES)
