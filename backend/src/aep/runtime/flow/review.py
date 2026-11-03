"""The node ``llm_review`` (模型审查): another model call checks what an earlier step
produced against a checklist (docs/api/flow-apps.md, "llm_review 模型审查").

A long prompt that carries a rule among many is easily ignored (jadeflow 4th retest:
"资料写明还没定的事写成问题" was in the plan prompt and the model did not do it);
a reviewer asked only about the checklist is far more reliable. What it finds goes
back to the step that wrote it, as a check node's problems do (branch ``retry``); after
``max_attempts`` reviews in a row with findings the run goes on and the findings are
shown to the person who confirms next (``on_stuck: pass``), or a person decides
(``branch``), or the node fails (``fail``). Any app can use it: a reply checked
against "不能承诺退款时间", a summary against "金额要和原文一致", an ETL plan against
a project's conventions.
"""

import json
from typing import Any

from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    ID,
    ID_RULE,
    CheckEnv,
    NodeFailure,
    NodeType,
    Outcome,
    Problems,
    Runtime,
    _ask_json,
    _keys,
    _model_and_skills,
    _text,
)

STUCK = "stuck"
ON_STUCK = ("pass", "branch", "fail")
MAX_RULES = 20
MAX_FINDINGS = 20
MAX_TITLE = 100
MAX_INSTRUCTION = 2000
MAX_ATTEMPTS = 5

FORMAT = """你是审查员。只按「审查清单」逐条检查「要审查的内容」：确实违反某一条时才记一条问题；\
清单以外的事不管，拿不准的不记，不要重写内容。

只输出一个 JSON 对象，不要输出其他文字：
{"findings": [{"rule": "清单里的标识", "where": "要审查的内容里出问题的位置，如 规则 r5、检查 \
ck_x、函数 build 里读 table1 的那句", "problem": "哪里不符合这一条，一两句话", "quote": "依据的原话\
（资料里的或内容里的，可以为空）", "fix": "怎么改，一句话"}]}
没有问题时输出 {"findings": []}。同一处违反同一条只记一次。"""


# Checking settings


def _check_rules(node: dict[str, Any], p: Problems) -> None:
    rules = node.get("rules")
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        p.add(("rules",), f"审查清单要有 1–{MAX_RULES} 条")
        return
    seen: set[str] = set()
    for i, rule in enumerate(rules):
        if isinstance(rule, str):
            # A reference to a list of rules, e.g. {{project.code_rules}}.
            if not refs.find(rule):
                p.add(("rules", i), "清单里的文字要引用一组规则，比如 {{project.code_rules}}")
            continue
        if not isinstance(rule, dict):
            p.add(("rules", i), "每条规则必须是对象，或者引用一组规则")
            continue
        key = rule.get("key")
        if not isinstance(key, str) or not ID.match(key):
            p.add(("rules", i, "key"), "标识" + ID_RULE)
        elif key in seen:
            p.add(("rules", i, "key"), f"标识 {key} 重复")
        else:
            seen.add(key)
        _text(rule, "title", p, "规则名称", required=True, max_length=MAX_TITLE, loc=("rules", i))
        _text(rule, "instruction", p, "怎么判断", max_length=MAX_INSTRUCTION, loc=("rules", i))


async def check_review(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    await _model_and_skills(node, p, env)
    _text(node, "subject", p, "要审查的内容", required=True)
    _text(node, "context", p, "参考资料")
    _text(node, "instruction", p, "补充说明")
    _check_rules(node, p)
    attempts = node.get("max_attempts", 2)
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or not 1 <= attempts <= MAX_ATTEMPTS
    ):
        p.add(("max_attempts",), f"连续几次没改过来 max_attempts 要在 1–{MAX_ATTEMPTS} 之间")
    if node.get("on_stuck", "pass") not in ON_STUCK:
        p.add(
            ("on_stuck",),
            "没改过来之后 on_stuck 只能是 pass（放行，把问题交给后面确认的人）、"
            "branch（交给人决定）或 fail（报错停下）",
        )


def branches(node: dict[str, Any]) -> list[str]:
    return ["ok", "retry", *([STUCK] if node.get("on_stuck") == "branch" else [])]


# Running


def rules_of(node: dict[str, Any], get: refs.Lookup) -> list[dict[str, str]]:
    """The checklist: the node's own rules, and the rules its references name (each a
    ``{key, title, instruction}``, or text taken as a rule's title); keys made unique."""
    found: list[dict[str, Any]] = []
    for rule in node.get("rules") or []:
        value = refs.resolve(rule, get)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and str(item.get("title") or "").strip():
                found.append(item)
            elif isinstance(item, str) and item.strip():
                found.append({"title": item.strip()})
    out: list[dict[str, str]] = []
    used: set[str] = set()
    for n, item in enumerate(found, start=1):
        key = str(item.get("key") or "")
        if not ID.match(key) or key in used:
            key = f"rule_{n}"
            while key in used:
                key += "_"
        used.add(key)
        out.append(
            {
                "key": key,
                "title": str(item["title"]).strip()[:MAX_TITLE],
                "instruction": str(item.get("instruction") or "").strip()[:MAX_INSTRUCTION],
            }
        )
    return out[:MAX_RULES]


def _checklist(rules: list[dict[str, str]]) -> str:
    return "\n".join(
        f"- 〔{r['key']}〕{r['title']}" + (f"：{r['instruction']}" if r["instruction"] else "")
        for r in rules
    )


def readable(value: Any) -> str:
    """What is reviewed, as a reviewer reads it: text as it is; files or code by name (an
    object of texts, such as the code of each target table) one after another; anything
    else as indented JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value and all(isinstance(v, str) for v in value.values()):
        return "\n\n".join(f"### {k}\n\n{v.strip()}" for k, v in value.items())
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2)


def _text_of(value: Any) -> str:
    return str(value).strip() if isinstance(value, str | int | float) else ""


def _findings(data: dict[str, Any], rules: list[dict[str, str]]) -> list[dict[str, str]]:
    titles = {r["key"]: r["title"] for r in rules}
    out = []
    for item in data["findings"][:MAX_FINDINGS]:
        out.append(
            {
                "rule": item["rule"],
                "title": titles[item["rule"]],
                "where": _text_of(item.get("where")),
                "problem": _text_of(item.get("problem")),
                "quote": _text_of(item.get("quote")),
                "fix": _text_of(item.get("fix")),
            }
        )
    return out


def _line(f: dict[str, str]) -> str:
    where = f"（{f['where']}）" if f["where"] else ""
    line = f"〔{f['title']}〕{where}{f['problem']}"
    if f["fix"]:
        line += f" 怎么改：{f['fix']}"
    if f["quote"]:
        line += f"（依据：{f['quote']}）"
    return line


def _report(findings: list[dict[str, str]], reviews: int, title: str) -> str:
    """For the person who looks next, when the findings were not fixed (Markdown)."""
    lines = [
        f"### {title}：没改过来的问题",
        "",
        f"按审查清单连续审查了 {reviews} 次，下面的问题模型都没改过来，请留意，需要时写意见：",
        "",
    ]
    for f in findings:
        where = f"（{f['where']}）" if f["where"] else ""
        line = f"- **{f['title']}**{where}：{f['problem']}"
        if f["fix"]:
            line += f" 建议：{f['fix']}"
        if f["quote"]:
            line += f"（依据：{f['quote']}）"
        lines.append(line)
    return "\n".join(lines)


async def run_review(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.model_lookup(state)
    rules = rules_of(node, get)
    title = node.get("title") or "模型审查"
    keys = ["findings", "problems", "report"]
    if not rules:
        # A project without conventions, say: nothing to review against.
        said = f"{title}：没有审查清单，跳过"
        return Outcome(
            {"ok": True, "findings": [], "problems": "", "report": "", "summary": said},
            "ok",
            messages=[{"role": "assistant", "content": said}],
            private={"failures": 0},
        )
    context = refs.render(node.get("context") or "", get).strip()
    lone = refs.whole(node["subject"])
    subject = readable(get(*lone)) if lone else refs.render(node["subject"], get)
    system = rt.system(node, FORMAT, refs.render(node.get("instruction") or "", get))
    user = "审查清单：\n" + _checklist(rules)
    if context:
        user += "\n\n参考资料：\n" + context
    user += "\n\n要审查的内容：\n" + subject
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    known = _keys(rules)

    def valid(data: Any) -> str | None:
        if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
            return '要的是 {"findings": [...]}'
        for item in data["findings"]:
            if not isinstance(item, dict):
                return "findings 里每一项都要是对象"
            if item.get("rule") not in known:
                return "rule 只能是清单里的标识：" + "、".join(known)
            if not _text_of(item.get("problem")):
                return "每一项都要写 problem"
        return None

    data, restored = rt.unmask(await _ask_json(rt, node, messages, valid, state), state)
    findings = _findings(data, rules)
    mine = (state.get("private") or {}).get(node["id"]) or {}
    failures = mine.get("failures", 0) + 1 if findings else 0
    most = node.get("max_attempts", 2)
    problems = "\n".join(f"- {_line(f)}" for f in findings)
    classified = rt.classify(state, [node["subject"], node.get("context") or ""], keys)
    outputs = {"ok": not findings, "findings": findings, "problems": problems, "report": ""}
    if not findings:
        said = f"{title}：按 {len(rules)} 条清单审查，没有发现问题"
        return Outcome(
            outputs | {"summary": said},
            "ok",
            messages=[{"role": "assistant", "content": said}],
            private={"failures": 0},
            restored=restored,
            **classified,
        )
    if failures < most:
        said = (
            f"{title}发现 {len(findings)} 处问题，让上一步带着问题重写（第 {failures}/{most} 次）"
        )
        return Outcome(
            outputs | {"summary": said},
            "retry",
            messages=[{"role": "assistant", "content": said}],
            private={"failures": failures},
            restored=restored,
            **classified,
        )
    on_stuck = node.get("on_stuck", "pass")
    if on_stuck == "fail":
        raise NodeFailure(
            "output_format",
            f"{title}连续 {failures} 次发现问题：" + "；".join(_line(f) for f in findings),
        )
    report = _report(findings, failures, title)
    if on_stuck == "branch":
        said = f"{title}连续 {failures} 次发现问题，交给人决定"
        branch = STUCK
    else:
        said = f"{title}连续 {failures} 次发现问题，没改过来，交给后面确认的人看"
        branch = "ok"
        # From here the person decides: a later rewrite (for their comment) is not sent
        # back to these problems.
        outputs["problems"] = ""
    # After a person looked, the step has its rewrites again.
    return Outcome(
        outputs | {"report": report, "summary": said},
        branch,
        messages=[{"role": "assistant", "content": said}],
        private={"failures": 0},
        restored=restored,
        **classified,
    )


TYPES = {
    "llm_review": NodeType(
        "llm_review",
        "模型审查",
        "另调一次模型，只按一份清单审查前面某一步的产出；有问题就让那一步带着问题重写",
        "model",
        "model",
        check_review,
        run_review,
        branches,
        lambda node: ["ok", "findings", "problems", "report", "summary"],
        outputs=(
            ("ok", "最后一次审查有没有发现问题（没有为 true）"),
            ("findings", "发现的问题：规则、位置、问题、依据、怎么改"),
            ("problems", "发现的问题，一条一行，给重写的那一步"),
            ("report", "没改过来时给人看的说明（Markdown）；改好了或没问题时为空"),
            ("summary", "一句话总结"),
        ),
        defaults={
            "model": None,
            "skills": [],
            "subject": "",
            "context": "",
            "instruction": "",
            "rules": [{"key": "rule_1", "title": "", "instruction": ""}],
            "max_attempts": 2,
            "on_stuck": "pass",
        },
        error_handling=True,
        branch_labels=lambda node: {"ok": "通过", "retry": "重写", STUCK: "交给人"},
    )
}
