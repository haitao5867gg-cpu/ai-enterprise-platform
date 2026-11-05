#!/usr/bin/env python3
"""Real-model acceptance of agent apps and conversations on a deployed platform.

Runs on the server after a deploy (docs/api/agent-apps.md, conversations.md): creates a few apps
named 「验收·…」, talks to them with the platform's default model and checks multi-turn
history, an agent calling an app, saving a turn as an eval case and evaluating it, the
「允许被其他应用调用」 switch, usage, a flow app with conversation on, converting to a flow
app and grounded-only answers. Standard library only.

    python3 infra/vps/accept-agents.py [report.md]   # on the server: API at 127.0.0.1:8000
    AEP_SITE_URL=https://<site> AEP_SITE_USER=… AEP_SITE_PASSWORD=… \
        python3 infra/vps/accept-agents.py            # from elsewhere, through the site

The site password is read from the environment and never printed. Keeps the agent app, the
app it calls and the main conversation so they can be looked at on the pages, archives the
rest (KEEP=1 keeps all). Exits 0 when every check passes.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Through the site's /api proxy when a site is given, else straight to the API on this machine.
SITE = os.environ.get("AEP_SITE_URL", "").rstrip("/")
USER = os.environ.get("AEP_SITE_USER", "")
PASSWORD = os.environ.get("AEP_SITE_PASSWORD", "")
API = (
    os.environ.get("AEP_API_URL") or (f"{SITE}/api" if SITE else "http://127.0.0.1:8000")
).rstrip("/")
KEEP_ALL = os.environ.get("KEEP") == "1"
SUFFIX = time.strftime("%m%d%H%M", time.gmtime())
REQUESTER = "验收脚本"

lines: list[str] = []
failures: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    lines.append(text)


def check(ok: bool, what: str, detail: str = "") -> bool:
    mark = "通过" if ok else "不通过"
    say(f"- [{mark}] {what}" + (f"：{detail}" if detail else ""))
    if not ok:
        failures.append(what)
    return ok


def short(text: Any, n: int = 160) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Problem(Exception):
    pass


def call(
    method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 202, 204)
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{API}/{path.lstrip('/')}", data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if USER or PASSWORD:
        token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    if status not in ok:
        raise Problem(f"{method} /api/{path} → {status} {short(raw.decode(errors='replace'), 400)}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


def wait_run(run_id: str, limit: float = 420) -> dict:
    end = time.time() + limit
    while True:
        run = call("GET", f"runs/{run_id}")
        if run["status"] not in ("queued", "running"):
            return run
        if time.time() > end:
            raise Problem(f"运行 {run_id} 超过 {int(limit)} 秒还没结束")
        time.sleep(3)


def turn(conversation_id: str, message: str) -> dict:
    started = time.time()
    made = call(
        "POST",
        f"conversations/{conversation_id}/messages",
        {"message": message, "requester": REQUESTER},
    )
    run = wait_run(made["run_id"])
    run["_seconds"] = round(time.time() - started, 1)
    run["_turn"] = made.get("turn")
    return run


def reply_of(run: dict) -> str:
    result = run.get("result") or {}
    return str(result.get("reply") or "") if isinstance(result, dict) else ""


def steps_of(run: dict) -> list[dict]:
    result = run.get("result") or {}
    steps = result.get("steps") if isinstance(result, dict) else None
    return steps if isinstance(steps, list) else []


def new_app(app: str, title: str, summary: str, origin: dict) -> dict:
    return call(
        "POST",
        "apps",
        {"app": app, "title": title, "summary": summary, "from": origin, "actor": REQUESTER},
    )


def save_and_publish(app: str, config: dict, note: str) -> dict:
    call("PUT", f"apps/{app}/draft", {"config": config, "actor": REQUESTER})
    return call("POST", f"apps/{app}/draft/publish", {"note": note, "actor": REQUESTER})


def chat_flow(prompt: str, conversation: dict | None) -> dict:
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "inputs": [{"key": "message", "label": "消息", "type": "textarea", "required": True}],
        },
        {
            "id": "ask",
            "type": "llm",
            "title": "回答",
            "prompt": prompt,
            "history": conversation is not None,
        },
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "reply", "from": ["{{ask.text}}"]}],
        },
    ]
    config: dict[str, Any] = {
        "graph": {
            "nodes": nodes,
            "edges": [{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
        }
    }
    if conversation is not None:
        config["conversation"] = conversation
    return config


def main() -> int:
    say(f"# 智能体应用真实模型验收（{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC）")
    say()
    say(f"接口：{API}")
    archive: list[str] = []
    keep: list[str] = []

    # 0. The server runs a build with agent apps.
    say()
    say("## 0. 服务器版本")
    templates = call("GET", "app-templates")
    kinds = {t["id"]: t.get("kind") for t in templates}
    if not check(
        kinds.get("agent-blank") == "agent",
        "模板里有智能体应用（agent-blank）",
        f"共 {len(templates)} 个模板",
    ):
        raise Problem("服务器还没升级到带智能体应用的版本")
    detail = call("GET", "apps/hello")
    say(f"- 平台默认模型：{detail.get('options', {}).get('default_model')}")

    # 1. A flow app the agent may call: translate to English.
    say()
    say("## 1. 被调用的编排应用")
    callee = f"accept-translate-{SUFFIX}"
    new_app(callee, "验收·翻译成英文", "把一段中文翻译成英文", {"blank": True})
    save_and_publish(
        callee,
        chat_flow("把下面这段中文翻译成英文，只输出译文，不要解释：\n{{start.message}}", None),
        "验收：翻译",
    )
    alone = call(
        "POST",
        "runs",
        {"app": callee, "input": {"message": "今天下午三点开会"}, "requester": REQUESTER},
    )
    alone = wait_run(alone["run_id"])
    check(
        alone["status"] == "done",
        "编排应用单独运行",
        short((alone.get("result") or {}).get("reply")),
    )
    keep.append(callee)

    # 2. The agent app.
    say()
    say("## 2. 智能体应用：多轮对话、调用应用")
    agent = f"accept-agent-{SUFFIX}"
    made = new_app(
        agent, "验收·部门助手", "回答部门内部问题，需要时调用翻译应用", {"blank_agent": True}
    )
    check(made.get("kind") == "agent", "新建空白智能体应用", f"kind={made.get('kind')}")
    config = json.loads(json.dumps(made["current"]["config"]))
    config["agent"].update(
        {
            "instructions": (
                "你是部门内部的助手，用中文简短、准确地回答。"
                "用户要把中文翻译成英文时，调用翻译应用，把它的译文原样告诉用户。"
                "做算术时写出算式。"
            ),
            "apps": [{"app": callee, "when": "用户要求把一段中文翻译成英文时", "enabled": True}],
            "grounded_only": False,
            "max_steps": 6,
            "history_turns": 10,
        }
    )
    config["chat"] = {"opening": "你好，我是部门助手。", "suggestions": ["帮我把一句话翻译成英文"]}
    published = save_and_publish(agent, config, "验收：多轮 + 调用应用")
    check(published["current"]["version"] == 2, "保存草稿并发布为版本 2")
    access = call("GET", f"apps/{callee}/access")
    callers = [c.get("app") for c in access.get("apps", {}).get("callers", [])]
    check(
        agent in callers,
        "被调应用的「接入」列出调用它的应用",
        short(access.get("apps", {}).get("callers")),
    )
    keep.append(agent)

    conv = call("POST", "conversations", {"app": agent, "requester": REQUESTER})
    cid = conv["conversation_id"]
    check(
        conv.get("turn_count") == 0 and conv.get("title") == "新对话",
        "开会话（还没有消息）",
        f"{cid} · {conv.get('title')}",
    )

    t1 = turn(cid, "记住：我们部门的出差住宿标准是每晚 450 元。")
    check(t1["status"] == "done", f"第 1 轮（{t1['_seconds']} 秒）", short(reply_of(t1)))
    t2 = turn(cid, "按刚才说的标准，住 3 晚一共多少钱？请写出计算过程。")
    r2 = reply_of(t2)
    check(
        t2["status"] == "done" and "1350" in r2.replace(",", "").replace("，", ""),
        f"第 2 轮靠历史算出 1350（{t2['_seconds']} 秒）",
        short(r2),
    )
    t3 = turn(cid, "把“库存不足，请尽快补货”翻译成英文。")
    r3 = reply_of(t3)
    calls = [s for s in steps_of(t3) if s.get("tool") == "call_app"]
    check(t3["status"] == "done", f"第 3 轮（{t3['_seconds']} 秒）", short(r3))
    if check(
        bool(calls) and calls[0].get("app") == callee and not calls[0].get("error"),
        "第 3 轮调用了翻译应用",
        short(calls[:1]),
    ):
        child = call("GET", f"runs/{calls[0]['run_id']}")
        trigger = child.get("trigger") or {}
        check(
            child["status"] == "done"
            and trigger.get("kind") == "app"
            and trigger.get("run_id") == t3["run_id"],
            "被调运行记下是谁调的",
            f"trigger={short(trigger)}",
        )
    check("stock" in r3.lower(), "回答里有英文译文", short(r3))

    full = call("GET", f"conversations/{cid}")
    turns = full.get("turns", [])
    check(
        len(turns) == 3 and [t["turn"] for t in turns] == [1, 2, 3],
        "会话列出 3 轮",
        f"标题「{full.get('title')}」",
    )
    renamed = call("PATCH", f"conversations/{cid}", {"title": "验收：住宿标准和翻译"})
    check(renamed.get("title") == "验收：住宿标准和翻译", "会话改名")
    listed = call("GET", f"conversations?app={agent}")
    check(any(c["conversation_id"] == cid for c in listed), "会话列表里有它")
    marks = [(t.get("conversation") or {}) for t in (t1, t2, t3)]
    check(
        [m.get("conversation_id") for m in marks] == [cid] * 3
        and [m.get("turn") for m in marks] == [1, 2, 3],
        "每轮的运行记着会话和轮次",
    )

    # 3. Feedback, a case from a turn, and an eval on the draft.
    say()
    say("## 3. 点赞、存为评测用例、评测")
    call("PUT", f"runs/{t2['run_id']}/feedback", {"rating": "good"})
    case = call("POST", f"apps/{agent}/eval-set/cases/from-run", {"run_id": t2["run_id"]})
    check(
        len(case.get("history") or []) == 1 and case.get("turn") == 2,
        "第 2 轮存成用例，带上第 1 轮历史",
        f"turn={case.get('turn')} · 检查 {short(case.get('expect'), 200)}",
    )
    call("PUT", f"apps/{agent}/draft", {"config": config, "actor": REQUESTER})
    call("POST", f"apps/{agent}/draft/eval", {})
    end = time.time() + 600
    while True:
        evaluation = (call("GET", f"apps/{agent}").get("draft") or {}).get("eval") or {}
        if evaluation.get("status") not in (None, "running") or time.time() > end:
            break
        time.sleep(5)
    check(
        evaluation.get("status") == "passed",
        "草稿评测（用例按会话的下一轮跑）",
        f"{evaluation.get('status')} · 得分 {evaluation.get('score')}"
        f" / 门槛 {evaluation.get('threshold')}"
        + (f" · {evaluation.get('error')}" if evaluation.get("error") else ""),
    )
    call("DELETE", f"apps/{agent}/draft")

    # 4. Entry switch: callee closed to other apps → the agent no longer gets it as a tool.
    say()
    say("## 4. 入口开关")
    call("PUT", f"apps/{callee}/access", {"apps": False})
    side = call("POST", "conversations", {"app": agent, "requester": REQUESTER})
    t4 = turn(side["conversation_id"], "把“明天放假”翻译成英文。")
    check(
        t4["status"] == "done" and not [s for s in steps_of(t4) if s.get("tool") == "call_app"],
        "关掉「允许被其他应用调用」后，智能体不再调用它",
        short(reply_of(t4)),
    )
    call("PUT", f"apps/{callee}/access", {"apps": True})
    call("DELETE", f"conversations/{side['conversation_id']}")
    gone = call("GET", f"conversations/{side['conversation_id']}", ok=(404,))
    check(True, "删除会话后按不存在处理（404）", short(gone))

    # 5. Usage.
    say()
    say("## 5. 使用情况")
    usage = call("GET", f"apps/{agent}/usage?days=7")
    convs = usage.get("conversations") or {}
    check(
        usage["totals"].get("runs", 0) >= 4 and (usage.get("entries") or {}).get("use", 0) >= 4,
        "运行数和按入口分",
        f"totals.runs={usage['totals'].get('runs')} · entries={short(usage.get('entries'))}",
    )
    check(convs.get("count", 0) >= 2, "会话数和平均轮数", short(convs))
    check(
        any(a.get("app") == callee for a in usage.get("apps") or []),
        "被调应用的次数",
        short(usage.get("apps")),
    )
    check((usage.get("feedback") or {}).get("good", 0) >= 1, "点赞", short(usage.get("feedback")))
    say(f"- 其他：answers={short(usage.get('answers'))} · tools={short(usage.get('tools'))}")

    # 6. A flow app with conversation on (chatflow).
    say()
    say("## 6. 编排应用打开对话")
    flow = f"accept-chatflow-{SUFFIX}"
    new_app(flow, "验收·对话编排", "开了对话的编排应用", {"blank": True})
    save_and_publish(
        flow,
        chat_flow("{{start.message}}", {"enabled": True, "history_turns": 10, "reply": "reply"}),
        "验收：对话",
    )
    fc = call("POST", "conversations", {"app": flow, "requester": REQUESTER})
    f1 = turn(fc["conversation_id"], "我叫王小明，请记住我的名字，回答“好的”即可。")
    f2 = turn(fc["conversation_id"], "我叫什么名字？")
    check(
        f1["status"] == "done" and f2["status"] == "done" and "王小明" in reply_of(f2),
        "第 2 轮记得第 1 轮说的名字",
        short(reply_of(f2)),
    )
    archive.append(flow)

    # 7. Convert the agent app to a flow app.
    say()
    say("## 7. 转成编排应用")
    converted = f"accept-agent-flow-{SUFFIX}"
    made = call(
        "POST",
        f"apps/{agent}/convert-to-flow",
        {"app": converted, "title": "验收·部门助手（编排）", "actor": REQUESTER},
    )
    check(
        made.get("kind") == "flow",
        "复制出编排应用",
        f"节点 {[n['id'] for n in made.get('nodes', [])]}",
    )
    cc = call("POST", "conversations", {"app": converted, "requester": REQUESTER})
    c1 = turn(cc["conversation_id"], "把“会议改到周五”翻译成英文。")
    check(
        c1["status"] == "done" and any(s.get("tool") == "call_app" for s in steps_of(c1)),
        "转出来的应用照样能对话、调用应用",
        short(reply_of(c1)),
    )
    archive.append(converted)

    # 8. Only answer from tools: a question the tools cannot answer.
    say()
    say("## 8. 只依据资料回答")
    strict = f"accept-grounded-{SUFFIX}"
    made = new_app(strict, "验收·只依据资料", "只依据被调应用的结果回答", {"blank_agent": True})
    strict_config = json.loads(json.dumps(made["current"]["config"]))
    strict_config["agent"].update(
        {
            "instructions": "你是部门内部的助手。只有翻译成英文这一件事可以做。",
            "apps": [{"app": callee, "when": "用户要求把一段中文翻译成英文时", "enabled": True}],
            "grounded_only": True,
        }
    )
    save_and_publish(strict, strict_config, "验收：只依据资料")
    sc = call("POST", "conversations", {"app": strict, "requester": REQUESTER})
    s1 = turn(sc["conversation_id"], "我们公司年假有几天？")
    grounded = (s1.get("result") or {}).get("grounded")
    check(
        s1["status"] == "done" and grounded is False,
        "查不到依据时换成固定回答",
        f"grounded={grounded} · {short(reply_of(s1))}",
    )
    archive.append(strict)

    # Cleanup.
    say()
    say("## 清理")
    for app in [] if KEEP_ALL else archive:
        call("DELETE", f"apps/{app}")
    say(f"- 保留：{', '.join(keep + (archive if KEEP_ALL else []))}（会话 {cid}）")
    if not KEEP_ALL:
        say(f"- 已归档：{', '.join(archive)}")
    return 0


if __name__ == "__main__":
    report = sys.argv[1] if len(sys.argv) > 1 else None
    code = 0
    try:
        main()
    except Problem as e:
        say()
        say(f"**中断**：{e}")
        code = 2
    except Exception as e:  # noqa: BLE001 - the report must say what stopped it
        say()
        say(f"**中断**：{type(e).__name__}: {e}")
        code = 2
    say()
    if code:
        say("**结果**：中断" + (f"，之前已有 {len(failures)} 项不通过" if failures else ""))
    elif failures:
        say(f"**结果**：不通过 {len(failures)} 项：" + "；".join(failures))
    else:
        say("**结果**：全部通过")
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    sys.exit(code or (1 if failures else 0))
