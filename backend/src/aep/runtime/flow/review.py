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

A reviewer is kept to what it is for (jadeflow 5th retest I-20, I-21): a finding it marks
as no violation, without a fix, or that says itself nothing needs changing is dropped;
and with ``decided`` (what people already decided in this run) it may not report those
decisions as problems or suggest undoing them, and checks they were all carried out.
"""

import hashlib
import json
import re
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
    decisions_made,
)

STUCK = "stuck"
ON_STUCK = ("pass", "branch", "fail")
MAX_RULES = 20
MAX_FINDINGS = 20
MAX_TITLE = 100
MAX_INSTRUCTION = 2000
MAX_ATTEMPTS = 5
# What changed since the last review is shown up to this many characters; a bigger change
# is reviewed whole (jadeflow 6th retest I-24, I-20).
MAX_CHANGES = 8000
# An excerpt this short (without spaces and punctuation) is not checked against the content.
MIN_EXCERPT = 4

FORMAT = """你是审查员。只按「审查清单」逐条检查「要审查的内容」：确实违反某一条时才记一条问题；\
清单以外的事不管，拿不准的不记，不要重写内容。看过、觉得没问题的地方不要记，\
不要写「无需修改」「不记」「符合要求」这样的条目。

只输出一个 JSON 对象，不要输出其他文字：
{"findings": [{"rule": "清单里的标识", "violates": true, "where": "要审查的内容里出问题的位置，\
如 规则 r5、检查 ck_x、函数 build 里读 table1 的那句", \
"excerpt": "要审查的内容里出问题的那一小段原文，原样复制，一个字也不改；问题是缺了什么时写空", \
"problem": "哪里不符合这一条，一两句话", \
"quote": "依据的原话（资料里的或内容里的，可以为空）", "fix": "要怎么改，一句话，必须写"}]}
violates 写这一处是不是确实违反了这一条，只有 true 的才算问题。没有问题时输出 {"findings": []}。\
同一处违反同一条只记一次。"""

# With ``decided`` (jadeflow 5th retest I-20: the reviewer took the answers people gave for
# undecided matters and had them asked again; I-21: it suggested removing what a person
# had asked for, and the rewrite did).
DECIDED_FORMAT = """「人已经做的决定」是这次运行里人亲自做的决定\
（回答的问题、选的选项、写的意见），按时间先后排，同一件事以后面的为准。按这些决定写的内容不是问题：不要报成违反，\
也不要建议撤掉或改掉它们。"""
DECIDED_RULE = {
    "key": "decided",
    "title": "人已经做的决定都要做到",
    "instruction": "「人已经做的决定」里的每一条（同一件事以后面的为准），要审查的内容里都要做到："
    "人已经决定的事不能再拿出来问，人要求的改动不能没做，也不能被撤掉。没做到就是违反，"
    "where 写哪里没做到，quote 写那条决定的原话。",
}
# What outranks the reviewer (jadeflow 6th retest: the reviewer overruled the data checks,
# I-24; the plan validation, I-23; a person and its own earlier review, I-20), in order. A
# finding against one of them is shown to a person, not sent back for a rewrite.
AGAINST = {
    "decided": ("人已经做的决定", "和人已经做的决定相反"),
    "limits": ("平台限制", "违反平台限制"),
    "evidence": ("检查结果", "和检查结果相反"),
    "previous": ("上一次审查的意见", "推翻了上一次审查的意见"),
}
SCOPE_FORMAT = """「上一次审查之后改了的地方」列出了和你上一次审查的版本相比改了什么。\
只审改了的地方和改动影响到的地方；上一次审查过、这次没改的地方不要再报，除非它没做到「人已经做的决定」里\
上一次审查之后才有的决定。"""
# What a finding that is no finding says (I-21: "符合要求……无需修改", "此处判定 q3 已覆盖，
# 不记", "该点不构成违反"); "不符合要求" and "不记录" are findings.
NO_CHANGE = re.compile(
    r"无需(?:修改|改动|更改|调整|处理)|不需要?(?:修改|改动|更改)|不用改|不必改|可不改"
    r"|不记(?![录入账])|不构成|无违规|没有违规|未违反|没有违反|并未违反|不违反"
    r"|(?<!不)符合(?:要求|约定|清单|规则)|无问题"
)


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
    _text(node, "decided", p, "人已经做的决定")
    _text(node, "limits", p, "平台限制")
    _text(node, "evidence", p, "检查结果")
    changes = node.get("changes")
    if changes is not None:
        from aep.runtime.flow import diff

        if not isinstance(changes, dict) or set(changes) != {"lists"}:
            p.add(("changes",), "changes 要写成 {lists}，lists 同人工节点的 changes")
        elif refs.whole(node.get("subject") or "") is None:
            p.add(("changes",), "要审查的内容整段是一个变量时才能写 changes")
        else:
            for loc, message in diff.check_lists(changes["lists"]):
                p.add(("changes", *loc), message)
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


def _decided_rule(rules: list[dict[str, str]]) -> dict[str, str]:
    """The checklist item a node with ``decided`` gets first: its key made unique."""
    key = DECIDED_RULE["key"]
    used = {r["key"] for r in rules}
    while key in used:
        key += "_"
    return DECIDED_RULE | {"key": key}


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


def no_change(item: dict[str, Any]) -> bool:
    """Whether a finding is none: marked as no violation, without a fix, or saying itself
    that nothing needs changing."""
    if item.get("violates") is False or not _text_of(item.get("fix")):
        return True
    return any(NO_CHANGE.search(_text_of(item.get(k))) for k in ("problem", "fix"))


def _bare(text: str) -> str:
    """Text compared for an excerpt: lower case, without spaces and punctuation."""
    return re.sub(r"[\W_]+", "", text.lower())


def found_in(excerpt: str, content: str) -> bool:
    """Whether a finding's excerpt is in the reviewed content (jadeflow 6th retest I-24: a
    finding said the code had a backfill it did not have). Empty or very short excerpts
    are taken as they are."""
    wanted = _bare(excerpt)
    return len(wanted) < MIN_EXCERPT or wanted in _bare(content)


def _judged(
    data: dict[str, Any], content: str, against: list[str]
) -> tuple[list[tuple[int, str]], int, int]:
    """Which findings are findings, each with what it goes against ("" when nothing); how
    many say they need no change and how many quote what the content does not hold."""
    out: list[tuple[int, str]] = []
    ignored = unfounded = 0
    for i, item in enumerate(data["findings"]):
        if no_change(item):
            ignored += 1
        elif not found_in(_text_of(item.get("excerpt")), content):
            unfounded += 1
        else:
            side = _text_of(item.get("against"))
            # Not carrying out what a person decided is never overruled by anything.
            if side not in against or item["rule"] == DECIDED_RULE["key"]:
                side = ""
            out.append((i, side))
    return out[:MAX_FINDINGS], ignored, unfounded


def _findings(
    data: dict[str, Any], judged: list[tuple[int, str]], rules: list[dict[str, str]]
) -> list[dict[str, Any]]:
    titles = {r["key"]: r["title"] for r in rules}
    out = []
    for i, side in judged:
        item = data["findings"][i]
        out.append(
            {
                "rule": item["rule"],
                "title": titles[item["rule"]],
                "where": _text_of(item.get("where")),
                "excerpt": _text_of(item.get("excerpt")),
                "problem": _text_of(item.get("problem")),
                "quote": _text_of(item.get("quote")),
                "fix": _text_of(item.get("fix")),
                "against": side,
                "hint": bool(side),
            }
        )
    return out


def _line(f: dict[str, Any]) -> str:
    where = f"（{f['where']}）" if f["where"] else ""
    line = f"〔{f['title']}〕{where}{f['problem']}"
    if f["fix"]:
        line += f" 怎么改：{f['fix']}"
    if f["quote"]:
        line += f"（依据：{f['quote']}）"
    return line


def _item(f: dict[str, Any]) -> str:
    where = f"（{f['where']}）" if f["where"] else ""
    line = f"- **{f['title']}**{where}：{f['problem']}"
    if f["fix"]:
        line += f" 建议：{f['fix']}"
    if f["quote"]:
        line += f"（依据：{f['quote']}）"
    if f.get("against"):
        line += f"（{AGAINST[f['against']][1]}）"
    return line


def _report(
    findings: list[dict[str, Any]], hints: list[dict[str, Any]], reviews: int, title: str
) -> str:
    """For the person who looks next (Markdown): the findings that were not fixed, then
    the ones that went against what outranks the reviewer."""
    parts = []
    if findings:
        lines = [
            f"### {title}：没改过来的问题",
            "",
            f"按审查清单连续审查了 {reviews} 次，下面的问题模型都没改过来，请留意，需要时写意见：",
            "",
            *map(_item, findings),
        ]
        parts.append("\n".join(lines))
    if hints:
        lines = [
            f"### {title}：和人的决定、平台限制、检查结果或上一次审查相反的意见",
            "",
            "下面的意见和排在审查前面的依据相反，平台没有让模型照改，请你判断，需要时写意见：",
            "",
            *map(_item, hints),
        ]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _merged(held: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hints kept since a person last decided, the new ones added once."""
    seen = {(h["rule"], h["where"], h["problem"]) for h in held}
    out = list(held)
    for h in new:
        if (h["rule"], h["where"], h["problem"]) not in seen:
            seen.add((h["rule"], h["where"], h["problem"]))
            out.append(h)
    return out[:MAX_FINDINGS]


def _version(value: Any, lists: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What was reviewed, kept to tell what changed next time: the lists named in
    ``changes`` item by item (as the human node's ``changes``), the rest as text."""
    if lists and isinstance(value, dict):
        from aep.runtime.flow import diff

        rest = json.loads(json.dumps(value))
        for spec in lists:
            *head, last = spec["path"].split(".")
            holder = rest
            for part in head:
                holder = holder.get(part) if isinstance(holder, dict) else None
            if isinstance(holder, dict):
                holder.pop(last, None)
        text = json.dumps(rest, ensure_ascii=False, indent=2)
        return {"items": diff.items(value, lists), "text": text}
    return {"text": readable(value)}


def _text_changes(before: str, now: str) -> list[str]:
    import difflib

    lines = list(difflib.unified_diff(before.splitlines(), now.splitlines(), lineterm="", n=1))
    return [line for line in lines[2:] if not line.startswith("@@")]


def changes_text(
    before: dict[str, Any] | None, now: dict[str, Any], lists: list[dict[str, Any]] | None
) -> str:
    """What changed since the last review, for the reviewer ("" when nothing did)."""
    if before is None:
        return ""
    lines: list[str] = []
    if lists and "items" in now and "items" in before:
        from aep.runtime.flow import diff

        kinds = {"added": "新加", "removed": "去掉", "changed": "改了"}
        for c in diff.changes(before["items"], now["items"], lists, None):
            said = f"：{c['fields']}" if c["fields"] else ""
            lines.append(f"- {c['list']}{kinds[c['kind']]}「{diff.short(c['label'], 80)}」{said}")
    lines += _text_changes(before.get("text") or "", now.get("text") or "")
    return "\n".join(lines)


def _said(line: str) -> bool:
    """Whether a line of ``decided`` says more than which option was clicked (a human
    node's ``decisions`` line: "【第 2 轮 · 审核试算结果 · 按检查结果改代码】" says nothing)."""
    head, mark, rest = line.partition("】")
    return bool(rest.strip()) if mark and head.lstrip().startswith("【") else bool(line.strip())


def _inputs(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


async def run_review(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.model_lookup(state)
    base = rules_of(node, get)
    title = node.get("title") or "模型审查"
    keys = ["findings", "problems", "report"]
    decided = refs.render(node.get("decided") or "", get).strip()
    rules = [_decided_rule(base), *base] if decided else base
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
    limits = refs.render(node.get("limits") or "", get).strip()
    evidence = refs.render(node.get("evidence") or "", get).strip()
    instruction = refs.render(node.get("instruction") or "", get)
    lone = refs.whole(node["subject"])
    value = get(*lone) if lone else refs.render(node["subject"], get)
    subject = readable(value)
    lists = (node.get("changes") or {}).get("lists") if lone else None
    version = _version(value, lists)
    inputs = _inputs(context, limits, instruction, _checklist(base))

    mine = (state.get("private") or {}).get(node["id"]) or {}
    seen = mine.get("seen") if isinstance(mine.get("seen"), dict) else None
    humans = decisions_made(state)
    # A person decided since the last review: the step has its rewrites again, and what
    # was found before that person looked is no longer sent back (jadeflow 6th retest I-23).
    looked = "humans" in mine and humans != mine["humans"]
    held = [] if looked else list(mine.get("hints") or [])
    failures = 0 if looked else mine.get("failures", 0)
    before = set((seen or {}).get("decided", "").splitlines())
    new = [line for line in decided.splitlines() if line.strip() and line not in before]
    said_new = [line for line in new if _said(line)]
    same = seen is not None and seen.get("inputs") == inputs
    changed = changes_text(seen.get("version"), version, lists) if seen else ""
    sources = (("decided", decided), ("limits", limits), ("evidence", evidence))
    against = [k for k, v in sources if v]
    previous = (seen or {}).get("told") or ""
    if previous:
        against.append("previous")

    reused = same and not changed and not said_new
    restored: dict[str, str] = {}
    ignored = unfounded = 0
    if reused:
        # The same content against the same checklist: the same verdict, no model call
        # (jadeflow 6th retest I-24: the same code passed twice and failed the third time).
        assert seen is not None
        found = [dict(f) for f in seen.get("found") or []]
        told = previous
    else:
        asked = rules
        scope = ""
        if same and not changed:
            # Only new decisions: whether they were carried out is all there is to check.
            asked = [_decided_rule(base)]
            scope = (
                "\n\n内容和上一次审查时一样，没有改。只查上一次审查之后新加的这些决定有没有做到：\n"
                + "\n".join(said_new)
            )
        elif same and len(changed) <= MAX_CHANGES:
            scope = "\n\n上一次审查之后改了的地方：\n" + changed
        system = rt.system(node, _format(decided, against, bool(scope)), instruction)
        user = "审查清单：\n" + _checklist(asked)
        if context:
            user += "\n\n参考资料：\n" + context
        if decided:
            user += "\n\n人已经做的决定（按时间先后，同一件事以后面的为准）：\n" + decided
        if limits:
            user += "\n\n平台限制（内容必须遵守，你给的改法也不能违反）：\n" + limits
        if evidence:
            user += "\n\n检查结果（在真实数据或真实运行上得到的，比你的判断可靠）：\n" + evidence
        if previous:
            after = "；之后人看过并做了决定，以人的决定为准" if looked else ""
            user += f"\n\n上一次审查提的意见（上一步已经按这些改过{after}）：\n" + previous
        user += scope + "\n\n要审查的内容：\n" + subject
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        known = _keys(asked)

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

        raw = await _ask_json(rt, node, messages, valid, state)
        judged, ignored, unfounded = _judged(raw, subject, against)
        # What goes back to a model as the reviewer wrote it (its tokens kept); what people
        # read with the run's values back in.
        told = "\n".join(f"- {_line(f)}" for f in _findings(raw, judged, asked) if not f["hint"])
        data, restored = rt.unmask(raw, state)
        found = _findings(data, judged, asked)

    problems_of = [f for f in found if not f["hint"]]
    hints = _merged(held, [f for f in found if f["hint"]])
    asides = []
    if ignored:
        # Findings that said themselves nothing needs changing are not counted (I-21).
        asides.append(f"另有 {ignored} 条写着不用改，已忽略")
    if unfounded:
        asides.append(f"另有 {unfounded} 条引用的原文在内容里找不到，已忽略")
    if hints:
        asides.append(f"{len(hints)} 条意见和排在审查前面的依据相反，交给人看，没有让模型改")
    if reused:
        asides.insert(0, "内容和上次审查时一样，沿用上次的结论，没有再调模型")
    aside = f"（{'；'.join(asides)}）" if asides else ""
    failures = failures + 1 if problems_of else 0
    most = node.get("max_attempts", 2)
    problems = "\n".join(f"- {_line(f)}" for f in problems_of)
    classified = rt.classify(
        state,
        [
            node["subject"],
            node.get("context") or "",
            node.get("decided") or "",
            node.get("limits") or "",
            node.get("evidence") or "",
        ],
        keys,
    )
    keep = {
        "humans": humans,
        "hints": hints,
        "seen": {
            "version": version,
            "inputs": inputs,
            "decided": decided,
            "found": found,
            "told": told if problems_of else "",
        },
    }
    outputs = {
        "ok": not problems_of,
        "findings": problems_of + hints,
        "problems": problems,
        "report": _report([], hints, 0, title),
    }
    if not problems_of:
        said = f"{title}：按 {len(rules)} 条清单审查，没有发现问题{aside}"
        return Outcome(
            outputs | {"summary": said},
            "ok",
            messages=[{"role": "assistant", "content": said}],
            private=keep | {"failures": 0},
            restored=restored,
            **classified,
        )
    if failures < most:
        said = (
            f"{title}发现 {len(problems_of)} 处问题，"
            f"让上一步带着问题重写（第 {failures}/{most} 次）{aside}"
        )
        return Outcome(
            outputs | {"summary": said},
            "retry",
            messages=[{"role": "assistant", "content": said}],
            private=keep | {"failures": failures},
            restored=restored,
            **classified,
        )
    on_stuck = node.get("on_stuck", "pass")
    if on_stuck == "fail":
        raise NodeFailure(
            "output_format",
            f"{title}连续 {failures} 次发现问题：" + "；".join(_line(f) for f in problems_of),
        )
    report = _report(problems_of, hints, failures, title)
    if on_stuck == "branch":
        said = f"{title}连续 {failures} 次发现问题，交给人决定{aside}"
        branch = STUCK
    else:
        said = f"{title}连续 {failures} 次发现问题，没改过来，交给后面确认的人看{aside}"
        branch = "ok"
        # From here the person decides: a later rewrite (for their comment) is not sent
        # back to these problems.
        outputs["problems"] = ""
    # After a person looked, the step has its rewrites again.
    return Outcome(
        outputs | {"report": report, "summary": said},
        branch,
        messages=[{"role": "assistant", "content": said}],
        private=keep | {"failures": 0},
        restored=restored,
        **classified,
    )


def _format(decided: str, against: list[str], scoped: bool) -> str:
    """The reviewer's instructions: the reply's format, what people decided, what outranks
    the review and, after a first review, to look at what changed."""
    text = FORMAT
    if decided:
        text += "\n\n" + DECIDED_FORMAT
    if against:
        order = [AGAINST[k][0] for k in against if k != "previous"]
        sides = "；".join(f"{k}（{AGAINST[k][1]}）" for k in against)
        text += (
            "\n\n每条再写 against：这条的改法会不会和排在审查前面的依据相反，"
            f"写 {sides}；都不会就写空。"
        )
        if order:
            text += "依据有先后：" + " > ".join([*order, "审查清单"]) + "。"
        if "previous" in against:
            text += "上一次要加的这次要去、上一次要去的这次要加，就是推翻上一次的意见。"
        text += (
            "和排在前面的依据冲突的意见照样记下来，against 写冲突的那一项："
            "平台会把它交给人判断，不会让上一步照改。"
        )
    if scoped:
        text += "\n\n" + SCOPE_FORMAT
    return text


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
            ("findings", "发现的问题：规则、位置、问题、依据、怎么改；和前面依据相反的标为提示"),
            ("problems", "发现的问题，一条一行，给重写的那一步"),
            ("report", "给人看的说明（Markdown）：没改过来的问题和交给人判断的提示；都没有时为空"),
            ("summary", "一句话总结"),
        ),
        defaults={
            "model": None,
            "skills": [],
            "subject": "",
            "context": "",
            "instruction": "",
            "decided": "",
            "limits": "",
            "evidence": "",
            "rules": [{"key": "rule_1", "title": "", "instruction": ""}],
            "max_attempts": 2,
            "on_stuck": "pass",
        },
        error_handling=True,
        branch_labels=lambda node: {"ok": "通过", "retry": "重写", STUCK: "交给人"},
    )
}
