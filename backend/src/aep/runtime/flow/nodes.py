"""The node types of a flow app: what each one's settings are, checks and does.

Every type is a ``NodeType`` in ``TYPES``: the text ``GET /node-types`` shows, a
``check`` of a node's settings (problems are reported where they are), the
``branches`` its outgoing edges use, the fields it ``produces`` and ``run``, which
does the work in a run. Settings are documented in docs/api/flow-apps.md.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from openai import AsyncOpenAI

from aep import llm
from aep.runtime import human
from aep.runtime.flow import refs
from aep.skills import SkillContent, SkillStore

ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_TEXT = 20000
FIELD_TYPES = ("text", "textarea", "number", "boolean", "select", "datasource")
JSON_TYPES = ("string", "number", "boolean", "array", "object")
OPS = ("eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte", "empty", "not_empty")
ERROR_TYPES = (
    "timeout",
    "model_error",
    "output_format",
    "tool_error",
    "code_error",
    "sandbox_unavailable",
    "step_limit",
)
# Variables a node that failed and went on (``on_error.then`` default or branch) has.
ERROR_FIELDS = ("error_type", "error_message")
# Branch names the platform gives: no class matched, no rule held, the node failed.
OTHER, ELSE, ERROR = "other", "else", "error"
Loc = tuple[str | int, ...]


class NodeFailure(Exception):
    """A node could not do its work; ``on_error`` decides what happens next."""

    def __init__(self, type: str, message: str) -> None:
        super().__init__(message)
        self.type = type
        self.message = message


@dataclass
class Problems:
    """Problems with one node's settings, located under the node."""

    prefix: Loc
    found: list[tuple[list[str | int], str]] = field(default_factory=list)

    def add(self, loc: Loc, message: str) -> None:
        self.found.append(([*self.prefix, *loc], message))


@dataclass(frozen=True)
class CheckEnv:
    """What checking a node needs: the gateway's model aliases (None: unknown, any
    alias is taken) and the skills."""

    models: list[str] | None
    skills: SkillStore


@dataclass
class Runtime:
    """What running a node needs. ``skills`` holds the text of every skill version the
    app uses, loaded before the run; ``env`` the app's constants."""

    llm_client: AsyncOpenAI
    default_model: str
    skills: dict[tuple[str, int], SkillContent]
    env: dict[str, Any]

    def lookup(self, state: dict[str, Any]) -> refs.Lookup:
        values = state.get("vars") or {}

        def get(node: str, name: str) -> Any:
            if node == refs.ENV:
                return self.env.get(name)
            if node == refs.SECRET:
                raise NodeFailure("tool_error", "密钥只能用在工具参数里")
            return (values.get(node) or {}).get(name)

        return get

    def system(self, node: dict[str, Any], *extra: str) -> str:
        parts = [self.skills[(s["name"], s["version"])].text() for s in node.get("skills") or []]
        parts += [e.strip() for e in extra if e and e.strip()]
        return "\n\n".join(parts)

    async def chat(self, node: dict[str, Any], messages: list[dict[str, str]]) -> str:
        model = node.get("model") or self.default_model
        try:
            reply = await llm.chat(self.llm_client, messages, model=model)
        except TimeoutError as exc:
            raise NodeFailure("timeout", "模型没有在规定时间内回答") from exc
        except Exception as exc:  # noqa: BLE001 - any gateway failure is the model's
            raise NodeFailure("model_error", f"调用模型失败：{exc}") from exc
        return reply.text


@dataclass
class Outcome:
    outputs: dict[str, Any]
    branch: str | None = None
    messages: list[dict[str, str]] = field(default_factory=list)
    result: dict[str, Any] | None = None


Check = Callable[[dict[str, Any], Problems, CheckEnv], Awaitable[None]]
Run = Callable[[dict[str, Any], dict[str, Any], Runtime], Awaitable[Outcome]]


@dataclass(frozen=True)
class NodeType:
    type: str
    label: str
    description: str
    category: Literal["model", "logic", "tool", "human", "terminal"]
    # How the run's flow view draws it (see aep.runtime.design).
    kind: Literal["model", "tool", "human", "code", "terminal"]
    check: Check
    run: Run
    # The node's branch names; None: one outgoing edge without a branch.
    branches: Callable[[dict[str, Any]], list[str] | None]
    produces: Callable[[dict[str, Any]], list[str]]
    outputs: tuple[tuple[str, str], ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)
    presets: tuple[dict[str, Any], ...] = ()
    error_handling: bool = False
    max_count: int | None = None
    # Branch display names, for diagrams.
    branch_labels: Callable[[dict[str, Any]], dict[str, str]] = lambda node: {}

    def describe(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "label": self.label,
            "description": self.description,
            "category": self.category,
            "error_handling": self.error_handling,
            "outputs": [{"key": k, "description": d} for k, d in self.outputs],
            "defaults": self.defaults,
            "presets": list(self.presets),
            "max_count": self.max_count,
        }


# Checking settings


def _text(
    node: dict[str, Any],
    key: str,
    p: Problems,
    label: str,
    required: bool = False,
    max_length: int = MAX_TEXT,
    loc: Loc = (),
) -> None:
    value = node.get(key)
    if value is None and not required:
        return
    if not isinstance(value, str):
        p.add((*loc, key), f"{label}必须是文字")
    elif required and not value.strip():
        p.add((*loc, key), f"{label}不能为空")
    elif len(value) > max_length:
        p.add((*loc, key), f"{label}最多 {max_length} 个字符")


def _list(
    node: dict[str, Any], key: str, p: Problems, label: str, low: int, high: int
) -> list[Any]:
    value = node.get(key)
    if not isinstance(value, list):
        p.add((key,), f"{label}必须是列表")
        return []
    if not low <= len(value) <= high:
        p.add((key,), f"{label}要有 {low}–{high} 项")
    return value


def _keyed(
    items: list[Any], key: str, p: Problems, what: str, reserved: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """The items that are objects with a valid, unique ``key``; problems for the rest."""
    good, seen = [], set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            p.add((key, i), f"每个{what}必须是对象")
            continue
        k = item.get("key")
        if not isinstance(k, str) or not ID.match(k):
            p.add((key, i, "key"), "标识只能用小写字母、数字和下划线，以字母开头，最多 32 个字符")
        elif k in reserved:
            p.add((key, i, "key"), f"{k} 是保留的标识")
        elif k in seen:
            p.add((key, i, "key"), f"标识 {k} 重复")
        else:
            seen.add(k)
            good.append(item)
    return good


def _label(item: dict[str, Any], loc: Loc, p: Problems, max_length: int = 50) -> None:
    value = item.get("label")
    if not isinstance(value, str) or not value.strip():
        p.add((*loc, "label"), "名称不能为空")
    elif len(value) > max_length:
        p.add((*loc, "label"), f"名称最多 {max_length} 个字符")


async def _model_and_skills(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    model = node.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            p.add(("model",), "模型别名不能为空")
        elif env.models is not None and model not in env.models:
            p.add(("model",), f"模型网关上没有 {model}")
    chosen = node.get("skills", [])
    if not isinstance(chosen, list):
        p.add(("skills",), "skills 必须是列表")
        return
    seen = set()
    for i, ref in enumerate(chosen):
        name = ref.get("name") if isinstance(ref, dict) else None
        version = ref.get("version") if isinstance(ref, dict) else None
        if not isinstance(name, str) or not isinstance(version, int):
            p.add(("skills", i), "每项要有 name 和整数 version")
        elif name in seen:
            p.add(("skills", i), f"Skill {name} 重复")
        elif await env.skills.version(name, version) is None:
            p.add(("skills", i), f"Skill {name} 没有已发布的版本 {version}")
        else:
            seen.add(name)


def check_fields(
    node: dict[str, Any], key: str, p: Problems, types: tuple[str, ...] = FIELD_TYPES
) -> None:
    """Input fields (a start node's inputs, a human node's form)."""
    for i, f in enumerate(_keyed(_list(node, key, p, "字段", 1, 20), key, p, "字段")):
        loc = (key, node[key].index(f))
        del i
        _label(f, loc, p)
        kind = f.get("type")
        if kind not in types:
            p.add((*loc, "type"), "类型只能是 " + "、".join(types))
        if "required" in f and not isinstance(f["required"], bool):
            p.add((*loc, "required"), "required 必须是 true 或 false")
        if kind == "select":
            choices = f.get("choices")
            if not isinstance(choices, list) or not 1 <= len(choices) <= 50:
                p.add((*loc, "choices"), "下拉选项要有 1–50 项")
            else:
                for j, c in enumerate(choices):
                    if not isinstance(c, dict) or not isinstance(c.get("value"), str):
                        p.add((*loc, "choices", j), "每个选项要有文字 value 和 label")
        if "max_length" in f and f["max_length"] is not None:
            m = f["max_length"]
            if kind not in ("text", "textarea"):
                p.add((*loc, "max_length"), "只有文字类型可以设最大长度")
            elif not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= MAX_TEXT:
                p.add((*loc, "max_length"), f"最大长度要在 1–{MAX_TEXT} 之间")


def field_problem(f: dict[str, Any], value: Any) -> str | None:
    """Why ``value`` is not a valid answer to field ``f``, or None."""
    empty = value is None or (isinstance(value, str) and not value.strip())
    if empty:
        return f"{f['label']}不能为空" if f.get("required") else None
    kind = f.get("type")
    if kind in ("text", "textarea", "datasource"):
        if not isinstance(value, str):
            return f"{f['label']}必须是文字"
        if f.get("max_length") and len(value) > f["max_length"]:
            return f"{f['label']}最多 {f['max_length']} 个字符"
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return f"{f['label']}必须是数字"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return f"{f['label']}必须是 true 或 false"
    elif kind == "select" and value not in [c["value"] for c in f.get("choices") or []]:
        return f"{f['label']}不是可选的值"
    return None


def answers(
    fields: list[dict[str, Any]], given: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The values for ``fields`` (defaults filled in) and the problems by field key."""
    values, problems = {}, {}
    for f in fields:
        value = given.get(f["key"], f.get("default"))
        if (problem := field_problem(f, value)) is not None:
            problems[f["key"]] = problem
        values[f["key"]] = value
    return values, problems


def _first_text(fields: list[dict[str, Any]], values: dict[str, Any]) -> str:
    for f in fields:
        if f.get("type") in ("text", "textarea") and isinstance(values.get(f["key"]), str):
            return values[f["key"]]
    return ""


def _keys(items: Any) -> list[str]:
    return [i["key"] for i in items or [] if isinstance(i, dict) and isinstance(i.get("key"), str)]


# start


async def check_start(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    check_fields(node, "inputs", p)


async def run_start(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    fields = node.get("inputs") or []
    given = state.get("input") or {}
    values = {f["key"]: given.get(f["key"], f.get("default")) for f in fields}
    text = _first_text(fields, values)
    return Outcome(values, messages=[{"role": "user", "content": text}] if text else [])


# llm


async def check_llm(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    await _model_and_skills(node, p, env)
    _text(node, "system", p, "系统提示词")
    _text(node, "prompt", p, "提示词", required=True)
    output = node.get("output", {"kind": "text"})
    if not isinstance(output, dict) or output.get("kind") not in ("text", "json"):
        p.add(("output",), "输出方式只能是 text 或 json")
    elif output["kind"] == "json":
        fields = output.get("fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
            p.add(("output", "fields"), "JSON 输出要列出 1–20 个字段")
            return
        sub = Problems((*p.prefix, "output"))
        for f in _keyed(fields, "fields", sub, "字段"):
            if f.get("type") not in JSON_TYPES:
                sub.add(("fields", fields.index(f), "type"), "类型只能是 " + "、".join(JSON_TYPES))
        p.found += sub.found


def _json_fields(node: dict[str, Any]) -> list[dict[str, Any]] | None:
    output = node.get("output") or {}
    return output.get("fields") if output.get("kind") == "json" else None


def produces_llm(node: dict[str, Any]) -> list[str]:
    fields = _json_fields(node)
    return _keys(fields) if fields is not None else ["text"]


JSON_CHECK = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


async def _ask_json(
    rt: Runtime,
    node: dict[str, Any],
    messages: list[dict[str, str]],
    valid: Callable[[Any], str | None],
) -> dict[str, Any]:
    """The model's JSON reply; asked again once when it is not valid."""
    problem = ""
    for _ in range(2):
        text = await rt.chat(node, messages)
        try:
            data = llm.parse_json(text)
        except ValueError as exc:
            problem = str(exc)
        else:
            problem = valid(data) or ""
            if not problem:
                return data
        messages = [
            *messages,
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"回复不符合要求：{problem}。请只输出要求的 JSON 对象。"},
        ]
    raise NodeFailure("output_format", f"模型没有按格式回答：{problem}")


async def run_llm(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    fields = _json_fields(node)
    instruction = ""
    if fields is not None:
        lines = [f"- {f['key']}（{f['type']}）：{f.get('description', '')}" for f in fields]
        instruction = "只输出一个 JSON 对象，不要输出别的内容。字段：\n" + "\n".join(lines)
    system = rt.system(node, refs.render(node.get("system") or "", get), instruction)
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": refs.render(node["prompt"], get)})
    if fields is None:
        text = await rt.chat(node, messages)
        return Outcome({"text": text}, messages=[{"role": "assistant", "content": text}])

    def valid(data: Any) -> str | None:
        if not isinstance(data, dict):
            return "要的是 JSON 对象"
        for f in fields:
            if f["key"] not in data:
                return f"缺少字段 {f['key']}"
            if data[f["key"]] is not None and not JSON_CHECK[f["type"]](data[f["key"]]):
                return f"字段 {f['key']} 应该是 {f['type']}"
        return None

    data = await _ask_json(rt, node, messages, valid)
    outputs = {f["key"]: data[f["key"]] for f in fields}
    text = json.dumps(outputs, ensure_ascii=False)
    return Outcome(outputs, messages=[{"role": "assistant", "content": text}])


# classifier


async def check_classifier(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    await _model_and_skills(node, p, env)
    _text(node, "text", p, "要分类的文本", required=True)
    _text(node, "instruction", p, "补充说明")
    classes = _list(node, "classes", p, "类别", 1, 10)
    for c in _keyed(classes, "classes", p, "类别", reserved=(OTHER,)):
        loc = ("classes", classes.index(c))
        _label(c, loc, p)
        _text(c, "description", p, "说明", max_length=500, loc=loc)


def _classes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in node.get("classes") or [] if isinstance(c, dict)]


async def run_classifier(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    classes = _classes(node)
    lines = [f"- {c['key']}：{c.get('label', '')}（{c.get('description', '')}）" for c in classes]
    task = (
        "把用户给的文本归到下面的一个类别里：\n"
        + "\n".join(lines)
        + f"\n都不属于时用 {OTHER}。只输出 JSON："
        + '{"class": "类别标识"}'
    )
    system = rt.system(node, task, refs.render(node.get("instruction") or "", get))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": refs.render(node["text"], get)},
    ]

    def valid(data: Any) -> str | None:
        return (
            None if isinstance(data, dict) and isinstance(data.get("class"), str) else "缺少 class"
        )

    data = await _ask_json(rt, node, messages, valid)
    chosen = next((c for c in classes if c["key"] == data["class"]), None)
    key = chosen["key"] if chosen else OTHER
    return Outcome({"class": key, "label": chosen.get("label", key) if chosen else "都不是"}, key)


# condition


async def check_condition(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    rules = _list(node, "rules", p, "规则", 1, 10)
    for rule in _keyed(rules, "rules", p, "规则", reserved=(ELSE,)):
        loc = ("rules", rules.index(rule))
        _label(rule, loc, p)
        if rule.get("match", "all") not in ("all", "any"):
            p.add((*loc, "match"), "match 只能是 all 或 any")
        conditions = rule.get("conditions")
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 10:
            p.add((*loc, "conditions"), "每条规则要有 1–10 个条件")
            continue
        for j, c in enumerate(conditions):
            cloc = (*loc, "conditions", j)
            if not isinstance(c, dict):
                p.add(cloc, "每个条件必须是对象")
                continue
            if not isinstance(c.get("left"), str) or not c["left"].strip():
                p.add((*cloc, "left"), "要比较的变量不能为空")
            if c.get("op") not in OPS:
                p.add((*cloc, "op"), "比较方式只能是 " + "、".join(OPS))
            elif c["op"] not in ("empty", "not_empty") and "right" not in c:
                p.add((*cloc, "right"), "缺少比较的值")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def holds(op: str, left: Any, right: Any) -> bool:
    if op == "empty":
        return left is None or left == "" or left == [] or left == {}
    if op == "not_empty":
        return not holds("empty", left, right)
    if op in ("eq", "ne"):
        a, b = _number(left), _number(right)
        same = (
            a == b if a is not None and b is not None else refs.as_text(left) == refs.as_text(right)
        )
        return same if op == "eq" else not same
    if op in ("contains", "not_contains"):
        found = (
            right in left if isinstance(left, list) else refs.as_text(right) in refs.as_text(left)
        )
        return found if op == "contains" else not found
    a, b = _number(left), _number(right)
    if a is None or b is None:
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


async def run_condition(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    for rule in node["rules"]:
        results = [
            holds(c["op"], refs.resolve(c["left"], get), refs.resolve(c.get("right"), get))
            for c in rule["conditions"]
        ]
        if (all if rule.get("match", "all") == "all" else any)(results):
            return Outcome({"rule": rule["key"]}, rule["key"])
    return Outcome({"rule": ELSE}, ELSE)


# human


async def check_human(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    _text(node, "prompt", p, "提示语", required=True, max_length=2000)
    _text(node, "show", p, "给人看的内容")
    mode = node.get("mode", "approval")
    if mode == "approval":
        options = _list(node, "options", p, "选项", 2, 5)
        good = _keyed(options, "options", p, "选项")
        for o in good:
            _label(o, ("options", options.index(o)), p, max_length=20)
        required = node.get("comment_required", [])
        keys = _keys(good)
        if not isinstance(required, list) or any(k not in keys for k in required):
            p.add(("comment_required",), "必须写意见的选项要在选项里")
    elif mode == "form":
        check_fields(node, "fields", p, tuple(t for t in FIELD_TYPES if t != "datasource"))
    else:
        p.add(("mode",), "模式只能是 approval（确认）或 form（填写）")


def produces_human(node: dict[str, Any]) -> list[str]:
    if node.get("mode", "approval") == "form":
        return [*_keys(node.get("fields")), "actor"]
    return ["decision", "comment", "actor"]


async def run_human(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    details = {
        "type": "flow_human",
        "node": node["id"],
        "show": refs.render(node.get("show") or "", get),
    }
    prompt = refs.render(node["prompt"], get)
    if node.get("mode", "approval") == "form":
        fields = node["fields"]
        answer = human.ask(
            prompt,
            details,
            options=("submit",),
            kind="form",
            option_labels={"submit": "提交"},
            fields=fields,
        )
        values = answer.get("values") or {}
        said = "；".join(f"{f['label']}：{refs.as_text(values.get(f['key']))}" for f in fields)
        outputs = {**{f["key"]: values.get(f["key"]) for f in fields}, "actor": answer.get("actor")}
        return Outcome(outputs, messages=[{"role": "user", "content": said}])
    options = node["options"]
    labels = {o["key"]: o.get("label", o["key"]) for o in options}
    answer = human.ask(
        prompt,
        details,
        options=[o["key"] for o in options],
        comment_required=node.get("comment_required", []),
        option_labels=labels,
    )
    decision = answer.get("decision")
    comment = answer.get("comment")
    said = labels.get(decision, decision or "")
    if comment:
        said += f"：{comment}"
    outputs = {"decision": decision, "comment": comment, "actor": answer.get("actor")}
    return Outcome(outputs, decision, messages=[{"role": "user", "content": said}])


# template


async def check_template(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    _text(node, "template", p, "模板", required=True)


async def run_template(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    return Outcome({"text": refs.render(node["template"], rt.lookup(state))})


# end


async def check_end(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    outputs = _list(node, "outputs", p, "结果字段", 1, 20)
    for o in _keyed(outputs, "outputs", p, "结果字段"):
        loc = ("outputs", outputs.index(o))
        sources = o.get("from")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 10:
            p.add((*loc, "from"), "每个结果字段要列出 1–10 个变量")
            continue
        for j, s in enumerate(sources):
            if not isinstance(s, str) or refs.whole(s) is None:
                p.add((*loc, "from", j), "每项必须恰好是一个变量，如 {{answer.text}}")


async def run_end(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    result = {}
    for o in node["outputs"]:
        value = None
        for source in o["from"]:
            value = refs.resolve(source, get)
            if value is not None and value != "":
                break
        result[o["key"]] = value
    return Outcome({}, result=result)


def _none(node: dict[str, Any]) -> list[str] | None:
    return None


def _fixed(*names: str) -> Callable[[dict[str, Any]], list[str]]:
    return lambda node: list(names)


TYPES: dict[str, NodeType] = {
    t.type: t
    for t in (
        NodeType(
            "start",
            "开始",
            "定义运行时要填的输入",
            "terminal",
            "terminal",
            check_start,
            run_start,
            _none,
            lambda node: _keys(node.get("inputs")),
            defaults={
                "inputs": [
                    {"key": "question", "label": "问题", "type": "textarea", "required": True}
                ]
            },
            max_count=1,
        ),
        NodeType(
            "llm",
            "调用模型",
            "调一次模型，产出一段文字或几个字段",
            "model",
            "model",
            check_llm,
            run_llm,
            _none,
            produces_llm,
            outputs=(("text", "模型的回答（输出方式为文本时）"),),
            defaults={
                "model": None,
                "skills": [],
                "system": "",
                "prompt": "",
                "output": {"kind": "text"},
            },
            presets=(
                {
                    "key": "extract",
                    "label": "抽取字段",
                    "description": "从文本里抽取结构化字段，每个字段成为一个变量",
                    "settings": {
                        "prompt": "从下面的文本中抽取字段：\n",
                        "output": {
                            "kind": "json",
                            "fields": [{"key": "value", "type": "string", "description": ""}],
                        },
                    },
                },
            ),
            error_handling=True,
        ),
        NodeType(
            "classifier",
            "意图分类",
            "让模型把文本归到几类中的一类，每类走一条分支",
            "model",
            "model",
            check_classifier,
            run_classifier,
            lambda node: [*_keys(node.get("classes")), OTHER],
            _fixed("class", "label"),
            outputs=(("class", "类别标识，都不是时为 other"), ("label", "类别名称")),
            defaults={
                "model": None,
                "skills": [],
                "text": "",
                "instruction": "",
                "classes": [{"key": "class_1", "label": "类别 1", "description": ""}],
            },
            error_handling=True,
            branch_labels=lambda node: {
                **{c["key"]: c.get("label", c["key"]) for c in _classes(node) if "key" in c},
                OTHER: "都不是",
            },
        ),
        NodeType(
            "condition",
            "条件判断",
            "按规则走不同分支，不调模型",
            "logic",
            "code",
            check_condition,
            run_condition,
            lambda node: [*_keys(node.get("rules")), ELSE],
            _fixed("rule"),
            outputs=(("rule", "成立的规则标识，都不成立时为 else"),),
            defaults={
                "rules": [
                    {
                        "key": "rule_1",
                        "label": "规则 1",
                        "match": "all",
                        "conditions": [{"left": "", "op": "eq", "right": ""}],
                    }
                ]
            },
            branch_labels=lambda node: {
                **{
                    r["key"]: r.get("label", r["key"])
                    for r in node.get("rules") or []
                    if isinstance(r, dict) and "key" in r
                },
                ELSE: "否则",
            },
        ),
        NodeType(
            "human",
            "人工",
            "暂停运行，等人确认或补充信息",
            "human",
            "human",
            check_human,
            run_human,
            lambda node: (
                _keys(node.get("options")) if node.get("mode", "approval") == "approval" else None
            ),
            produces_human,
            outputs=(
                ("decision", "选了哪个选项（确认模式）"),
                ("comment", "意见（确认模式）"),
                ("actor", "处理人"),
            ),
            defaults={
                "mode": "approval",
                "prompt": "确认吗？",
                "show": "",
                "options": [
                    {"key": "approve", "label": "确认"},
                    {"key": "reject", "label": "驳回"},
                ],
                "comment_required": ["reject"],
            },
            presets=(
                {
                    "key": "form",
                    "label": "请人补充信息",
                    "description": "弹一个表单让人填写，填好的字段成为变量",
                    "settings": {
                        "mode": "form",
                        "prompt": "请补充信息",
                        "fields": [
                            {"key": "value", "label": "内容", "type": "text", "required": True}
                        ],
                    },
                },
            ),
            branch_labels=lambda node: {
                o["key"]: o.get("label", o["key"])
                for o in node.get("options") or []
                if isinstance(o, dict) and "key" in o
            },
        ),
        NodeType(
            "template",
            "文本拼接",
            "把几个变量拼成一段文字",
            "logic",
            "code",
            check_template,
            run_template,
            _none,
            _fixed("text"),
            outputs=(("text", "拼好的文字"),),
            defaults={"template": ""},
        ),
        NodeType(
            "end",
            "结束",
            "结束运行，给出结果",
            "terminal",
            "terminal",
            check_end,
            run_end,
            lambda node: [],
            lambda node: [],
            defaults={"outputs": [{"key": "answer", "from": []}]},
        ),
    )
}
