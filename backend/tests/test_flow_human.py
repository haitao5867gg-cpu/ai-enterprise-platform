"""The human node's extended settings: questions, notes on what the page shows, turning a
decision into ``changed_branch``, the record of each round, and the end node's outcome
(docs/api/flow-apps.md, "人工节点的扩展设置" and "人工节点在工作台上的样子")."""

import copy
import hashlib
import json

from aep.runtime import human
from aep.runtime.flow import builtin
from tests.conftest import said
from tests.test_flow_apps import Platform, end, flow, node, problems

Q1 = {
    "key": "q1",
    "label": "统计哪段时间？",
    "type": "select",
    "choices": [
        {"value": "all", "label": "源库现有的 12 个月"},
        {"value": "y2026", "label": "只看 2026 年"},
    ],
    "suggested": "all",
    "why": "需求没说时间范围",
    "affects": ["m1", "m4"],
}
Q2 = {"key": "q2", "label": "要不要去掉测试订单？", "type": "boolean", "suggested": True}
BAD = {
    "key": "q3",
    "label": "按哪个口径？",
    "type": "select",
    "choices": [{"value": "a", "label": "下单"}],
    "suggested": "b",
}
BRIEF = {
    "metrics": [{"id": "m4", "name": "客单价"}, {"id": "m5", "label": "订单数"}],
    "dimensions": [{"key": "city", "label": "城市"}],
    "uncited": ["m3"],
}


def reply(questions=None, brief=None, ok=False) -> str:
    """What the question-writing step answers."""
    return json.dumps(
        {
            "questions": [Q1, Q2] if questions is None else questions,
            "brief": BRIEF if brief is None else brief,
            "ok": ok,
        },
        ensure_ascii=False,
    )


def review(**settings) -> dict:
    return {
        "id": "review",
        "type": "human",
        "title": "确认方案",
        "mode": "approval",
        "prompt": "请检查方案，回答每道问题",
        "show": "{{ask.brief}}",
        "view": "etl_plan_review",
        "data": {"brief": "{{ask.brief}}", "previous": "{{review.record}}"},
        "fields": "{{ask.questions}}",
        "changed_branch": "revise",
        "options": [
            {"key": "approve", "label": "确认方案，开始做"},
            {"key": "revise", "label": "按我的意见修改方案"},
            {"key": "pass", "label": "直接通过", "requires": "{{ask.ok}}"},
            {"key": "stop", "label": "终止"},
        ],
        "comment_required": ["revise", "stop"],
        **settings,
    }


def reviewed(**settings) -> dict:
    """Ask questions, have them answered, ask again on a revision; two ways to end."""
    ask = {
        "id": "ask",
        "type": "llm",
        "title": "出题",
        "prompt": "{{start.question}}\n{{review.feedback}}",
        "output": {
            "kind": "json",
            "fields": [
                {"key": "questions", "type": "array", "description": "题"},
                {"key": "brief", "type": "object", "description": "说明"},
                {"key": "ok", "type": "boolean", "description": "能不能直接通过"},
            ],
        },
    }
    done = {**end("{{review.decision}}"), "outcome": "delivered"}
    stopped = {**end("{{review.feedback}}"), "id": "stopped", "title": "终止", "outcome": "stopped"}
    return flow(
        ask,
        review(**settings),
        done,
        stopped,
        edges=[
            {"from": "start", "to": "ask"},
            {"from": "ask", "to": "review"},
            {"from": "review", "branch": "approve", "to": "end"},
            {"from": "review", "branch": "pass", "to": "end"},
            {"from": "review", "branch": "revise", "to": "ask"},
            {"from": "review", "branch": "stop", "to": "stopped"},
        ],
    )


async def refused(p: Platform, run: dict, **body) -> tuple[int, object]:
    resp = await p.api.post(f"/runs/{run['run_id']}/resume", json=body)
    detail = resp.json()["detail"]
    if isinstance(detail, list):
        detail = {tuple(e["loc"]): e["msg"] for e in detail}
    return resp.status_code, detail


# Checking


async def test_extended_settings_are_checked_where_they_are():
    config = reviewed(
        view="Plan Review",
        data={"Brief": 1, **{f"k{i}": i for i in range(12)}},
        fields=[{**Q1, "default": "all"}, {**Q2, "suggested": "yes"}, {**BAD}, Q1, "?"],
        changed_branch="pass",
    )
    node(config, "review")["options"][0]["requires"] = "开"
    node(config, "end")["outcome"] = "Delivered!"
    found = await problems(config)
    at = ("config", "graph", "nodes", "review")
    assert found == {
        (*at, "view"): "页面样式只能用小写字母、数字和下划线，以字母开头，最多 32 个字符",
        (*at, "data"): "data 最多 12 个键",
        (*at, "data", "Brief"): "键名只能用小写字母、数字和下划线，以字母开头，最多 32 个字符",
        (*at, "options", 0, "requires"): (
            "requires 只能是 true、false，或恰好是一个变量，如 {{dev_run.ok}}"
        ),
        (*at, "changed_branch"): "按意见处理的选项不能设 requires",
        (*at, "fields", 0, "default"): "题不能有默认值，每题都要人来答",
        (*at, "fields", 1, "suggested"): "建议的答案不是这题能填的值",
        (*at, "fields", 2, "suggested"): "建议的答案不在选项里",
        (*at, "fields", 3, "key"): "标识 q1 重复",
        (*at, "fields", 4): "每道题必须是对象",
        ("config", "graph", "nodes", "end", "outcome"): (
            "运行结果只能用小写字母、数字和下划线，以字母开头，最多 32 个字符"
        ),
    }

    config = reviewed(fields={"q1": Q1}, changed_branch="maybe")
    for option in node(config, "review")["options"]:
        option["requires"] = True
    node(config, "review")["comment_required"] = []
    found = await problems(config)
    assert found == {
        (*at, "fields"): "题目必须是列表，或恰好是一个变量，如 {{check_plan.questions}}",
        (*at, "options"): "至少要有一个选项不设 requires，总是显示",
        (*at, "changed_branch"): "按意见处理的选项要在选项里",
    }

    config = reviewed(fields="{{ask.questionz}} ", changed_branch=None)
    node(config, "review")["options"][2]["requires"] = "{{ask.okay}}"
    found = await problems(config)
    assert found == {
        (*at, "fields"): "节点 ask 没有产出 questionz",
        (*at, "options", 2, "requires"): "节点 ask 没有产出 okay",
    }


async def test_only_an_approval_can_turn_or_hide_options():
    config = reviewed()
    form = node(config, "review")
    form |= {
        "mode": "form",
        "fields": [{"key": "note", "label": "备注", "type": "text"}],
        "options": [{"key": "x", "label": "X", "requires": True}],
    }
    config["graph"]["edges"] = [
        e for e in config["graph"]["edges"] if e["from"] != "review" or e["branch"] == "approve"
    ]
    del config["graph"]["edges"][-1]["branch"]
    config["graph"]["nodes"] = [n for n in config["graph"]["nodes"] if n["id"] != "stopped"]
    node(config, "end")["outputs"][0]["from"] = ["{{review.note}}"]
    node(config, "ask")["prompt"] = "{{start.question}}"
    at = ("config", "graph", "nodes", "review")
    assert await problems(config) == {
        (*at, "changed_branch"): "只有确认模式可以设",
        (*at, "options", 0, "requires"): "只有确认模式可以设",
    }


async def test_null_settings_and_what_an_approval_produces():
    config = reviewed(view=None, data=None, fields=None, changed_branch=None)
    for option in node(config, "review")["options"]:
        option["requires"] = None
    node(config, "stopped")["outcome"] = None
    # Every output an approval has can be used further on.
    node(config, "stopped")["outputs"][0]["from"] = [
        "{{review.feedback}}",
        "{{review.record}}",
        "{{review.notes}}",
        "{{review.values}}",
        "{{review.changed}}",
    ]
    async with Platform([]) as p:
        await p.new_app("plan", config)
        types = {t["type"]: t for t in (await p.api.get("/node-types")).json()}
        outputs = [o["key"] for o in types["human"]["outputs"]]
        assert outputs == [
            "decision",
            "comment",
            "actor",
            "values",
            "notes",
            "changed",
            "feedback",
            "record",
        ]
        assert types["human"]["defaults"] == builtin_human_defaults()


def builtin_human_defaults() -> dict:
    return {
        "mode": "approval",
        "prompt": "确认吗？",
        "show": "",
        "options": [{"key": "approve", "label": "确认"}, {"key": "reject", "label": "驳回"}],
        "comment_required": ["reject"],
    }


# Running


async def test_a_plain_human_node_waits_as_before():
    config = copy.deepcopy(builtin.TEMPLATES["intent-routing"].config)
    async with Platform([json.dumps({"class": "after_sales"}), "第一稿"]) as p:
        await p.new_app("support", config)
        run = await p.run("support", {"question": "我要退货"})
        pending = run["pending"]
        assert pending["changed_branch"] is None
        assert pending["fields"] is None
        assert pending["details"] == {"type": "flow_human", "node": "review", "show": "第一稿"}
        # No questions, notes or turn: the decision reads as it always did.
        run = await p.resume(run, decision="approve", values={"x": 1}, seen="sha256:无关")
        assert said(run["messages"])[-1] == ("user", "review", None, "选了「确认」")
        assert run["status"] == "done" and run["outcome"] is None
        listed = (await p.api.get("/runs")).json()
        assert listed[0]["outcome"] is None


async def test_questions_notes_and_a_turned_decision():
    brief = {"metrics": [{"id": "m5", "label": "订单数"}]}
    async with Platform([reply(questions=[Q1, Q2, BAD, "?"]), reply(brief=brief, ok=True)]) as p:
        await p.new_app("plan", reviewed())
        run = await p.run("plan", {"question": "按城市统计 2026 年的客单价"})
        assert run["status"] == "waiting"
        pending = run["pending"]
        # ``pass`` needs ``ask.ok``, which is false now: it is not offered.
        assert pending["options"] == ["approve", "revise", "stop"]
        assert pending["option_labels"] == {
            "approve": "确认方案，开始做",
            "revise": "按我的意见修改方案",
            "stop": "终止",
        }
        assert pending["comment_required"] == ["revise", "stop"]
        assert pending["changed_branch"] == "revise"
        assert pending["fields"] == [{**Q1, "required": True}, {**Q2, "required": True}]
        details = pending["details"]
        assert details["view"] == "etl_plan_review"
        assert details["data"] == {"brief": BRIEF, "previous": None}
        assert details["round"] == 1
        assert details["dropped_fields"] == [
            {"index": 2, "key": "q3", "problem": "建议的答案不在选项里"},
            {"index": 3, "key": None, "problem": "每道题必须是对象"},
        ]
        shown = {
            **pending,
            "details": {k: v for k, v in details.items() if k not in ("seen", "round")},
        }
        text = json.dumps(shown, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        assert details["seen"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        assert human.fingerprint(pending) == details["seen"]
        seen = details["seen"]

        assert await refused(p, run, decision="pass") == (422, "这一步现在不能选「pass」")
        assert await refused(p, run, decision="approve", seen="sha256:旧的") == (
            409,
            "内容已经更新，请刷新后再处理",
        )
        status, detail = await refused(p, run, decision="approve", values={"q1": "y2025"})
        assert status == 422
        assert detail == {
            ("body", "values", "q1"): "统计哪段时间？不是可选的值",
            ("body", "values", "q2"): "要不要去掉测试订单？不能为空",
        }
        answered = {"q1": "y2026", "q2": True}
        notes = [
            {"item": "metric:m9", "text": "没有这个指标"},
            {"item": "m4", "text": "没有类别"},
            {"item": "metric:m4", "text": "  "},
            {"item": "Metric:m4", "text": "类别写错了"},
        ]
        status, detail = await refused(p, run, decision="approve", values=answered, notes=notes)
        assert status == 422
        assert detail == {
            ("body", "notes", 0, "item"): "页面上没有「metric:m9」这一项，挂不了意见",
            ("body", "notes", 1, "item"): "页面上没有「m4」这一项，挂不了意见",
            ("body", "notes", 2, "text"): "意见要写 1–500 个字，不能只有空格",
            ("body", "notes", 3, "item"): "页面上没有「Metric:m4」这一项，挂不了意见",
        }
        many = [{"item": "metric:m4", "text": "x"}] * 51
        status, detail = await refused(p, run, decision="approve", values=answered, notes=many)
        assert (status, detail) == (422, {("body", "notes"): "最多挂 50 条意见"})
        long = [{"item": "metric:m4", "text": "长" * 501}]
        status, detail = await refused(p, run, decision="approve", values=answered, notes=long)
        assert (status, detail) == (
            422,
            {("body", "notes", 0, "text"): "意见要写 1–500 个字，不能只有空格"},
        )
        status, _ = await refused(p, run, decision="approve", actor="长" * 101)
        assert status == 422

        # A changed answer and a note turn "approve" into "revise".
        run = await p.resume(
            run,
            decision="approve",
            values={**answered, "extra": 1},
            notes=[{"item": "metric:m4", "text": "保留两位小数"}],
            comment="",
            actor="韩梅梅",
            seen=seen,
        )
        assert said(run["messages"])[-1] == (
            "user",
            "review",
            "韩梅梅",
            "点了「确认方案，开始做」，改了答案或写了意见，按「按我的意见修改方案」处理；"
            "「统计哪段时间？」选了「只看 2026 年」（原建议「源库现有的 12 个月」）；"
            "另挂了 1 条意见",
        )
        feedback = (
            "【第 1 轮 · 确认方案 · 按我的意见修改方案】"
            "「统计哪段时间？」选「只看 2026 年」（原建议「源库现有的 12 个月」）；"
            "指标「客单价」：保留两位小数"
        )
        assert p.gateway.requests[1]["body"]["messages"][-1]["content"].endswith(feedback)

        # Round 2: the record of round 1 is shown, and ``pass`` is offered now.
        assert run["status"] == "waiting"
        pending = run["pending"]
        assert pending["options"] == ["approve", "revise", "pass", "stop"]
        assert pending["details"]["round"] == 2
        record = pending["details"]["data"]["previous"]
        assert record.pop("at")
        assert record == {
            "node": "review",
            "title": "确认方案",
            "round": 1,
            "actor": "韩梅梅",
            "decision": "revise",
            "decision_label": "按我的意见修改方案",
            "submitted": "approve",
            "seen": seen,
            "comment": "",
            "changed": True,
            "notes": [{"item": "metric:m4", "label": "客单价", "text": "保留两位小数"}],
            "answers": [
                {
                    "key": "q1",
                    "label": "统计哪段时间？",
                    "value": "y2026",
                    "value_label": "只看 2026 年",
                    "suggested": "all",
                    "suggested_label": "源库现有的 12 个月",
                    "picked_suggested": False,
                },
                {
                    "key": "q2",
                    "label": "要不要去掉测试订单？",
                    "value": True,
                    "value_label": "是",
                    "suggested": True,
                    "suggested_label": "是",
                    "picked_suggested": True,
                },
            ],
            "dropped_fields": [
                {"index": 2, "key": "q3", "problem": "建议的答案不在选项里"},
                {"index": 3, "key": None, "problem": "每道题必须是对象"},
            ],
        }
        # m4 is only in the old record now, which notes do not reach.
        status, detail = await refused(
            p,
            run,
            decision="revise",
            values={"q1": "all", "q2": True},
            notes=[{"item": "metric:m4", "text": "还是两位小数"}],
        )
        assert (status, detail) == (
            422,
            {("body", "notes", 0, "item"): "页面上没有「metric:m4」这一项，挂不了意见"},
        )

        run = await p.resume(run, decision="pass", values={"q1": "all", "q2": True}, actor="韩梅梅")
        assert run["status"] == "done"
        assert run["outcome"] == "delivered"
        assert run["result"] == {"out": "pass"}
        assert said(run["messages"])[-1] == ("user", "review", "韩梅梅", "选了「直接通过」")
        steps = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()["steps"]
        last = [s for s in steps if s["node"] == "review"][-1]["output"]["vars"]["review"]
        assert (last["decision"], last["feedback"], last["changed"]) == ("pass", "", False)
        assert last["values"] == {"q1": "all", "q2": True}
        assert last["record"]["submitted"] is None
        listed = (await p.api.get("/runs")).json()
        assert (listed[0]["status"], listed[0]["outcome"]) == ("done", "delivered")


async def test_a_decision_that_needs_a_reason_takes_a_comment_a_note_or_a_changed_answer():
    async with Platform([reply()] * 4) as p:
        await p.new_app("plan", reviewed())
        suggested = {"q1": "all", "q2": True}

        run = await p.run("plan", {"question": "统计客单价"})
        assert await refused(p, run, decision="stop", values=suggested, comment="  ") == (
            422,
            "这个决定需要写明意见",
        )
        run = await p.resume(run, decision="stop", values=suggested, comment="不做了")
        assert run["status"] == "done"
        assert run["outcome"] == "stopped"
        assert run["result"] == {"out": "【第 1 轮 · 确认方案 · 终止】意见：不做了"}
        assert said(run["messages"])[-1] == ("user", "review", None, "选了「终止」；意见：不做了")

        # A changed answer is reason enough, and a decision that needs one is not turned.
        run = await p.run("plan", {"question": "统计客单价"})
        run = await p.resume(run, decision="stop", values={"q1": "y2026", "q2": True})
        assert run["outcome"] == "stopped"
        assert run["result"] == {
            "out": "【第 1 轮 · 确认方案 · 终止】「统计哪段时间？」选「只看 2026 年」"
            "（原建议「源库现有的 12 个月」）"
        }

        # So is a note; a blank comment is none, and does not turn "approve".
        run = await p.run("plan", {"question": "统计客单价"})
        dimension = [{"item": "dimension:city", "text": "按区县"}]
        run = await p.resume(run, decision="stop", values=suggested, notes=dimension)
        assert run["result"] == {"out": "【第 1 轮 · 确认方案 · 终止】维度「城市」：按区县"}

        run = await p.run("plan", {"question": "统计客单价"})
        run = await p.resume(run, decision="approve", values=suggested, comment=" ")
        assert run["result"] == {"out": "approve"}
        assert said(run["messages"])[-1] == ("user", "review", None, "选了「确认方案，开始做」")


async def test_a_form_shows_its_data_but_takes_no_notes():
    config = flow(
        {
            "id": "ask",
            "type": "human",
            "title": "补充信息",
            "mode": "form",
            "prompt": "请补充",
            "view": "order_form",
            "data": {"question": "{{start.question}}", "items": ["订单号"]},
            "fields": [{"key": "order_id", "label": "订单号", "type": "text", "required": True}],
        },
        end("{{ask.order_id}}"),
        edges=[{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
    )
    async with Platform([]) as p:
        await p.new_app("form", config)
        run = await p.run("form", {"question": "查订单"})
        pending = run["pending"]
        assert pending["changed_branch"] is None
        details = pending["details"]
        assert details["data"] == {"question": "查订单", "items": ["订单号"]}
        assert (details["view"], details["round"], details["dropped_fields"]) == (
            "order_form",
            1,
            [],
        )
        assert details["seen"] == human.fingerprint(pending)
        note = [{"item": "field:订单号", "text": "格式不对"}]
        status, detail = await refused(
            p, run, decision="submit", values={"order_id": "A1"}, notes=note
        )
        assert (status, detail) == (422, {("body", "notes"): "填写的步骤不能挂意见"})
        run = await p.resume(run, decision="submit", values={"order_id": "A1"}, notes=[])
        assert run["result"] == {"out": "A1"}
        assert run["outcome"] is None


async def test_questions_written_in_the_node_and_what_a_reference_may_give():
    config = reviewed(fields=[Q1], data={"brief": "{{ask.brief}}"})
    node(config, "ask")["output"]["fields"][0]["type"] = "string"
    replies = [
        reply(questions="写在节点里的题不看这个"),
        reply(questions="不是题目"),
        reply(questions=""),
    ]
    async with Platform(replies) as p:
        await p.new_app("plan", config)
        run = await p.run("plan", {"question": "统计客单价"})
        assert run["pending"]["fields"] == [{**Q1, "required": True}]

        config = reviewed(data={"brief": "{{ask.brief}}"})
        node(config, "ask")["output"]["fields"][0]["type"] = "string"
        await p.new_app("plan2", config)
        run = await p.run("plan2", {"question": "统计客单价"})
        assert run["pending"]["fields"] is None
        assert run["pending"]["details"]["dropped_fields"] == [
            {"index": None, "key": None, "problem": "题目不是列表，这一步不带题"}
        ]
        run = await p.resume(run, decision="approve")
        assert said(run["messages"])[-1] == ("user", "review", None, "选了「确认方案，开始做」")

        run = await p.run("plan2", {"question": "统计客单价"})
        assert run["pending"]["fields"] is None
        assert run["pending"]["details"]["dropped_fields"] == []


async def test_questions_given_by_a_reference_lose_defaults_and_past_twenty_are_cut():
    # A default (refused when written in the node) is taken off, not held against it.
    many = [
        {**Q2, "key": f"q{i}", "label": f"第 {i} 题", "default": False, "required": False}
        for i in range(1, 23)
    ]
    async with Platform([reply(questions=many)]) as p:
        await p.new_app("plan", reviewed())
        run = await p.run("plan", {"question": "统计客单价"})
        fields = run["pending"]["fields"]
        assert [f["key"] for f in fields] == [f"q{i}" for i in range(1, 21)]
        assert fields[0] == {**Q2, "key": "q1", "label": "第 1 题", "required": True}
        assert run["pending"]["details"]["dropped_fields"] == [
            {"index": 20, "key": "q21", "problem": "最多 20 题，从第 21 题起不问"},
            {"index": 21, "key": "q22", "problem": "最多 20 题，从第 21 题起不问"},
        ]


# Finding what a note is on


def test_a_note_finds_its_item_in_a_fixed_order():
    data = {
        "uncited": ["m3", "m4"],
        "brief": {
            "metrics": [
                {"id": "m3", "name": "订单数", "label": ""},
                {"id": "m4", "title": "客单价"},
                {"key": "x" * 70, "text": "很" * 70},
            ],
        },
        "plan": {
            "targets": [
                {"name": "ads_sales", "columns": [{"name": "city"}, {"name": "gmv"}]},
                {"name": "ads_sales.city", "label": "同名的表"},
            ]
        },
        "files": [{"path": "src/pipeline.py"}],
        "previous": {"node": "review", "round": 1, "decision": "revise", "notes": [{"id": "old"}]},
    }
    label = human.note_label
    # Keys are taken in sorted order: "brief" comes before "uncited".
    assert label(data, "metric:m3") == "订单数"
    assert label(data, "metric:m4") == "客单价"
    assert label(data, "rule:" + "x" * 70) == "很" * 59 + "…"
    # An object named so comes before ``outer.inner``.
    assert label(data, "column:ads_sales.city") == "同名的表"
    assert label(data, "column:ads_sales.gmv") == "ads_sales.gmv"
    assert label(data, "file:src/pipeline.py") == "src/pipeline.py"
    assert label({"uncited": ["m3"]}, "metric:m3") == "m3"
    # A record is not looked into; nor is anything that is not there.
    assert label(data, "metric:old") is None
    assert label(data, "column:ads_sales.") is None
    assert label(data, "column:nothing.city") is None
    assert label(None, "metric:m3") is None
    for item in ("m3", ":m3", "Metric:m3", "metric:", "metric:" + "m" * 201):
        assert label(data, item) is None, item
