"""Checking a flow app's configuration before it is saved.

``check_config`` returns the configuration to keep or raises ``ConfigError`` with
every problem it found, each located the way docs/api/flow-apps.md describes:
under the node's id, so the editor can show it next to the setting.
"""

import copy
from collections import defaultdict
from typing import Any

from aep.runtime.appconfig import ConfigError
from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    ERROR,
    ERROR_FIELDS,
    ID,
    TYPES,
    CheckEnv,
    Problems,
)

SCHEMA_VERSION = 1
MAX_NODES = 50
MAX_EDGES = 200
MAX_VARS = 50
MAX_SECRETS = 20
DEFAULT_MAX_VISITS = 3
MAX_VISITS = 10
VAR_TYPES = {"string": str, "number": (int, float), "boolean": bool}
# Settings that hold no references.
NOT_SETTINGS = {"id", "type", "title", "ui", "on_error", "max_visits"}
# Nodes a loop must pass through to be able to leave it.
DECIDERS = {"classifier", "condition"}

Problem = tuple[list[str | int], str]


def error_fields(node: dict[str, Any]) -> list[str]:
    """``error_type`` and ``error_message`` when the node may fail and go on."""
    then = (node.get("on_error") or {}).get("then", "stop")
    return list(ERROR_FIELDS) if then in ("default", "branch") else []


def branches(node: dict[str, Any]) -> list[str | None]:
    """The node's outgoing edges by branch; None is the edge without a branch."""
    named = TYPES[node["type"]].branches(node)
    out: list[str | None] = [None] if named is None else list(named)
    if (node.get("on_error") or {}).get("then") == "branch":
        out.append(ERROR)
    return out


def _check_env(raw: Any, add: Any) -> dict[str, Any]:
    if raw is None:
        return {"vars": [], "secrets": []}
    if not isinstance(raw, dict):
        add(["env"], "env 必须是对象")
        return {"vars": [], "secrets": []}
    variables = raw.get("vars", [])
    if not isinstance(variables, list) or len(variables) > MAX_VARS:
        add(["env", "vars"], f"常量最多 {MAX_VARS} 个")
        variables = []
    seen = set()
    for i, v in enumerate(variables):
        loc = ["env", "vars", i]
        if not isinstance(v, dict):
            add(loc, "每个常量必须是对象")
            continue
        key = v.get("key")
        if not isinstance(key, str) or not ID.match(key):
            add([*loc, "key"], "名称只能用小写字母、数字和下划线，以字母开头，最多 32 个字符")
        elif key in seen:
            add([*loc, "key"], f"常量 {key} 重复")
        seen.add(key)
        kind = v.get("type")
        if kind not in VAR_TYPES:
            add([*loc, "type"], "类型只能是 string、number 或 boolean")
        else:
            value = v.get("value")
            wrong = not isinstance(value, VAR_TYPES[kind]) or (
                kind == "number" and isinstance(value, bool)
            )
            if wrong:
                add([*loc, "value"], f"值必须是 {kind}")
    secrets = raw.get("secrets", [])
    if not isinstance(secrets, list) or len(secrets) > MAX_SECRETS:
        add(["env", "secrets"], f"密钥最多 {MAX_SECRETS} 个")
        secrets = []
    for i, name in enumerate(secrets):
        if not isinstance(name, str) or not ID.match(name):
            add(["env", "secrets", i], "密钥名只能用小写字母、数字和下划线，以字母开头")
        elif secrets.index(name) != i:
            add(["env", "secrets", i], f"密钥 {name} 重复")
    return {"vars": variables, "secrets": secrets}


def check_common(node: dict[str, Any], p: Problems) -> None:
    title = node.get("title")
    if not isinstance(title, str) or not title.strip():
        p.add(("title",), "标题不能为空")
    elif len(title) > 50:
        p.add(("title",), "标题最多 50 个字符")
    visits = node.get("max_visits", DEFAULT_MAX_VISITS)
    if not isinstance(visits, int) or isinstance(visits, bool) or not 1 <= visits <= MAX_VISITS:
        p.add(("max_visits",), f"最多运行次数要在 1–{MAX_VISITS} 之间")
    if "ui" in node and not isinstance(node["ui"], dict):
        p.add(("ui",), "ui 必须是对象")
    on_error = node.get("on_error")
    if on_error is None:
        return
    spec = TYPES[node["type"]]
    if not spec.error_handling:
        p.add(("on_error",), f"{spec.label}节点不能设出错处理")
        return
    if not isinstance(on_error, dict):
        p.add(("on_error",), "on_error 必须是对象")
        return
    retry = on_error.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            p.add(("on_error", "retry"), "retry 必须是对象")
        else:
            times = retry.get("max", 0)
            if not isinstance(times, int) or isinstance(times, bool) or not 0 <= times <= 3:
                p.add(("on_error", "retry", "max"), "重试次数要在 0–3 之间")
            wait = retry.get("interval_seconds", 0)
            if isinstance(wait, bool) or not isinstance(wait, int | float) or not 0 <= wait <= 60:
                p.add(("on_error", "retry", "interval_seconds"), "重试间隔要在 0–60 秒之间")
    then = on_error.get("then", "stop")
    if then not in ("stop", "default", "branch"):
        p.add(("on_error", "then"), "只能是 stop、default 或 branch")
    elif then == "default":
        default = on_error.get("default")
        produced = spec.produces(node)
        if not isinstance(default, dict):
            p.add(("on_error", "default"), "用默认值继续时要给出默认值")
        else:
            for key in default:
                if key not in produced:
                    p.add(("on_error", "default", key), f"节点没有产出 {key}")


def _reach(start: str, succ: dict[str, list[str]]) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for nxt in succ.get(todo.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def _sccs(ids: list[str], succ: dict[str, list[str]]) -> list[list[str]]:
    """Strongly connected components (Tarjan), each in node order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on.add(v)
        for w in succ.get(v, []):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(sorted(comp, key=ids.index))

    for v in ids:
        if v not in index:
            visit(v)
    return out


def _dominators(start: str, nodes: set[str], pred: dict[str, list[str]]) -> dict[str, set[str]]:
    dom = {n: set(nodes) for n in nodes}
    dom[start] = {start}
    changed = True
    while changed:
        changed = False
        for n in nodes - {start}:
            ps = [dom[p] for p in pred.get(n, []) if p in nodes]
            new = {n} | (set.intersection(*ps) if ps else set())
            if new != dom[n]:
                dom[n] = new
                changed = True
    return dom


async def check_config(raw: Any, env: CheckEnv) -> dict[str, Any]:
    problems: list[Problem] = []

    def add(loc: list[str | int], message: str) -> None:
        problems.append((["config", *loc], message))

    def fail() -> ConfigError:
        return ConfigError(problems)

    if not isinstance(raw, dict):
        raise ConfigError([(["config"], "配置必须是对象")])
    if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        add(["schema_version"], f"不认识的配置格式版本，目前只有 {SCHEMA_VERSION}")
    environment = _check_env(raw.get("env"), add)
    graph = raw.get("graph")
    if not isinstance(graph, dict):
        add(["graph"], "缺少 graph")
        raise fail()
    nodes, edges = graph.get("nodes"), graph.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        add(["graph"], "graph 要有 nodes 和 edges 两个列表")
        raise fail()
    if len(nodes) > MAX_NODES:
        add(["graph"], f"最多 {MAX_NODES} 个节点")
    if len(edges) > MAX_EDGES:
        add(["graph"], f"最多 {MAX_EDGES} 条连线")

    by_id: dict[str, dict[str, Any]] = {}
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            add(["graph", "nodes", i], "每个节点必须是对象")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not ID.match(node_id):
            add(
                ["graph", "nodes", i, "id"],
                "标识只能用小写字母、数字和下划线，以字母开头，最多 32 个字符",
            )
            continue
        if node_id in (refs.ENV, refs.SECRET):
            add(["graph", "nodes", node_id, "id"], f"{node_id} 是保留的标识")
            continue
        if node_id in by_id:
            add(["graph", "nodes", node_id, "id"], f"标识 {node_id} 重复")
            continue
        p = Problems(("config", "graph", "nodes", node_id))
        spec = TYPES.get(node.get("type"))  # type: ignore[arg-type]
        if spec is None:
            p.add(("type",), "不认识的节点类型")
            problems.extend(p.found)
            continue
        check_common(node, p)
        await spec.check(node, p, env)
        problems.extend(p.found)
        by_id[node_id] = node

    for type_name, spec in TYPES.items():
        count = sum(1 for n in by_id.values() if n["type"] == type_name)
        if spec.max_count is not None and count > spec.max_count:
            add(["graph"], f"最多只能有 {spec.max_count} 个{spec.label}节点")
    starts = [n["id"] for n in by_id.values() if n["type"] == "start"]
    if not starts:
        add(["graph"], "要有一个开始节点")
    if not any(n["type"] == "end" for n in by_id.values()):
        add(["graph"], "至少要有一个结束节点")

    out: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for i, edge in enumerate(edges):
        loc: list[str | int] = ["graph", "edges", i]
        if not isinstance(edge, dict):
            add(loc, "每条连线必须是对象")
            continue
        source, target, branch = edge.get("from"), edge.get("to"), edge.get("branch")
        ok = True
        if source not in by_id:
            add([*loc, "from"], f"没有节点 {source}")
            ok = False
        if target not in by_id:
            add([*loc, "to"], f"没有节点 {target}")
            ok = False
        elif by_id[target]["type"] == "start":
            add([*loc, "to"], "不能连回开始节点")
            ok = False
        if branch is not None and not isinstance(branch, str):
            add([*loc, "branch"], "branch 必须是文字")
            ok = False
        if ok:
            out[source].append((i, edge))

    for node_id, node in by_id.items():
        spec = TYPES[node["type"]]
        expected = branches(node)
        labels = spec.branch_labels(node) | {ERROR: "出错"}
        seen: set[str | None] = set()
        for i, edge in out.get(node_id, []):
            branch = edge.get("branch")
            if node["type"] == "end":
                add(["graph", "edges", i, "from"], "结束节点不能有下一步")
            elif branch not in expected:
                what = f"分支 {branch}" if branch else "不带分支的下一步"
                add(["graph", "edges", i, "branch"], f"节点 {node_id} 没有{what}")
            elif branch in seen:
                add(["graph", "edges", i], f"节点 {node_id} 的这个出口连了不止一次")
            seen.add(branch)
        for branch in expected:
            if branch in seen:
                continue
            if branch is None:
                add(["graph", "nodes", node_id, "next"], "没有设置下一步")
            else:
                name = labels.get(branch, branch)
                add(
                    ["graph", "nodes", node_id, "branches", branch],
                    f"分支「{name}」没有连到下一个节点",
                )
    if problems:
        raise fail()

    ids = list(by_id)
    succ = {n: [e["to"] for _, e in out.get(n, [])] for n in ids}
    pred: dict[str, list[str]] = defaultdict(list)
    for n, targets in succ.items():
        for t in targets:
            pred[t].append(n)
    start = starts[0]
    reached = _reach(start, succ)
    ends = {n for n in ids if by_id[n]["type"] == "end"}
    back = set().union(*(_reach(e, pred) for e in ends))
    component: dict[str, int] = {}
    endless: set[str] = set()
    for k, comp in enumerate(_sccs(ids, succ)):
        for n in comp:
            component[n] = k
        looped = len(comp) > 1 or comp[0] in succ.get(comp[0], [])
        decides = any(
            by_id[n]["type"] in DECIDERS
            or (by_id[n]["type"] == "human" and by_id[n].get("mode", "approval") == "approval")
            for n in comp
        )
        if looped and not decides and comp[0] in reached:
            add(
                ["graph", "nodes", comp[0]],
                "这个循环没有经过人工确认、条件判断或意图分类节点，会一直循环下去",
            )
            endless.update(comp)
    for n in ids:
        if n not in reached:
            add(["graph", "nodes", n], "从开始走不到这个节点")
        elif n not in back and n not in endless:
            add(["graph", "nodes", n], "从这个节点走不到结束节点")
    if problems:
        raise fail()

    dom = _dominators(start, reached, pred)
    env_keys = {v["key"] for v in environment["vars"]}
    for node_id, node in by_id.items():
        settings = {k: v for k, v in node.items() if k not in NOT_SETTINGS}
        for loc, text in refs.strings(settings):
            for target, name in refs.find(text):
                where = ["graph", "nodes", node_id, *loc]
                if target == refs.ENV:
                    if name not in env_keys:
                        add(where, f"没有常量 {name}")
                elif target == refs.SECRET:
                    if node["type"] != "tool" or loc[:1] != ("args",):
                        add(where, "密钥只能写在工具节点的参数里")
                    elif name not in environment["secrets"]:
                        add(where, f"没有声明密钥 {name}，请先在应用的密钥里添加")
                elif target not in by_id:
                    add(where, f"没有节点 {target}")
                else:
                    other = by_id[target]
                    fields = TYPES[other["type"]].produces(other) + error_fields(other)
                    if name not in fields:
                        add(where, f"节点 {target} 没有产出 {name}")
                    # An end node takes the first of its sources that has a value, so
                    # it may name nodes on other branches.
                    elif node["type"] == "end":
                        continue
                    elif target not in dom[node_id] and component[target] != component[node_id]:
                        add(where, f"运行到这里时节点「{other['title']}」不一定运行过，不能引用它")
    if problems:
        raise fail()
    out_config = copy.deepcopy(raw)
    out_config["schema_version"] = SCHEMA_VERSION
    out_config["env"] = environment
    out_config["graph"].setdefault("edges", [])
    return out_config
