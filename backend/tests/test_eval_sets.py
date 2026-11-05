"""Eval sets of flow apps, feedback on runs, saving a run as a case, and the developer
view: each step's time and model use, and rerunning from a step (docs/api/evals.md)."""

import json

import pytest

from aep import eval_sets, llm
from aep.feedback import MemoryFeedbackStore
from aep.main import app
from aep.models import MemoryCallStore
from aep.runtime import catalog
from tests.test_flow_apps import Platform, flow, node

LONG = (
    "您好，退货申请已经收到，我们会在七个工作日内把款项原路退回，"
    "请留意银行卡的到账通知；如果超过时间还没有到账，请带上订单号联系售后客服。"
)


def support() -> dict:
    """Ask for the order, answer, have a person approve the answer."""
    return flow(
        {
            "id": "ask",
            "type": "human",
            "title": "补充订单",
            "mode": "form",
            "prompt": "请补充订单号",
            "fields": [{"key": "order_id", "label": "订单号", "type": "text", "required": True}],
        },
        {
            "id": "draft",
            "type": "llm",
            "title": "回答",
            "prompt": "{{start.question}} 订单 {{ask.order_id}} {{review.comment}}",
            "output": {"kind": "text"},
            "max_visits": 3,
        },
        {
            "id": "review",
            "type": "human",
            "title": "确认",
            "mode": "approval",
            "prompt": "确认吗？",
            "show": "{{draft.text}}",
            "options": [{"key": "approve", "label": "确认"}, {"key": "reject", "label": "驳回"}],
            "comment_required": ["reject"],
            "max_visits": 3,
        },
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [
                {"key": "answer", "from": ["{{draft.text}}"]},
                {"key": "order", "from": ["{{ask.order_id}}"]},
            ],
        },
        edges=[
            {"from": "start", "to": "ask"},
            {"from": "ask", "to": "draft"},
            {"from": "draft", "to": "review"},
            {"from": "review", "branch": "approve", "to": "end"},
            {"from": "review", "branch": "reject", "to": "draft"},
        ],
    )


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(catalog, "POLL_SECONDS", 0.01)


@pytest.fixture(autouse=True)
def feedback_store():
    app.state.feedback = MemoryFeedbackStore()
    yield
    app.state.feedback = None


async def finished_run(p: Platform, answer_first: str = "approve") -> dict:
    """A support run: the order given, the answer approved (after one rejection when
    ``answer_first`` is reject)."""
    run = await p.run("support", {"question": "我要退货"})
    run = await p.resume(run, decision="submit", values={"order_id": "SO-1"})
    if answer_first == "reject":
        run = await p.resume(run, decision="reject", comment="写清楚几天")
    return await p.resume(run, decision="approve")


# The eval set itself


async def test_an_eval_set_is_kept_and_checked():
    async with Platform(["回答"]) as p:
        await p.new_app("support", support())
        empty = (await p.api.get("/apps/support/eval-set")).json()
        assert empty | {"threshold": None} == {
            "app": "support",
            "source": None,
            "editable": True,
            "threshold": None,
            "judge_model": None,
            "cases": [],
            "updated_by": None,
            "updated_at": None,
        }
        assert (await p.api.get("/apps/support")).json()["has_eval_set"] is False

        case = {
            "id": "refund",
            "title": "  退货  ",
            "input": {"question": "我要退货"},
            "answers": {"ask": {"values": {"order_id": "SO-1"}}, "review": {"decision": "approve"}},
            "expect": [
                {"kind": "contains", "path": "answer", "values": ["退货", "七天|7 天"]},
                {"kind": "judge", "path": "answer", "criteria": "礼貌"},
            ],
        }
        body = {"threshold": 0.5, "judge_model": "qwen", "cases": [case]}
        saved = await p.api.put("/apps/support/eval-set", json=body, headers={"X-Actor": "x"})
        assert saved.status_code == 200, saved.text
        saved = saved.json()
        assert (saved["source"], saved["threshold"], saved["judge_model"]) == (
            "platform",
            0.5,
            "qwen",
        )
        [kept] = saved["cases"]
        assert kept["title"] == "退货"
        assert kept["answers"] == {
            "ask": {"values": {"order_id": "SO-1"}},
            "review": {"decision": "approve"},
        }
        assert kept["from_run"] is None and saved["updated_at"]
        assert (await p.api.get("/apps/support")).json()["has_eval_set"] is True

        # Leaving the threshold out keeps it.
        again = await p.api.put("/apps/support/eval-set", json={"cases": [case]})
        assert again.json()["threshold"] == 0.5

        bad = [
            ({"cases": [case, case]}, ["body", "cases", 1, "id"]),
            ({"cases": [case | {"id": "Bad Id"}]}, ["body", "cases", 0, "id"]),
            (
                {"cases": [case | {"expect": [{"kind": "contains", "values": []}]}]},
                ["body", "cases", 0, "expect", 0, "contains", "values"],
            ),
            (
                {"cases": [case | {"expect": [{"kind": "nope"}]}]},
                ["body", "cases", 0, "expect", 0],
            ),
            ({"cases": [], "threshold": 1.5}, ["body", "threshold"]),
            ({"cases": [case | {"extra": 1}]}, ["body", "cases", 0, "extra"]),
        ]
        for body, loc in bad:
            resp = await p.api.put("/apps/support/eval-set", json=body)
            assert resp.status_code == 422, body
            assert resp.json()["detail"][0]["loc"] == loc, resp.json()

        # Code apps keep theirs in the repository.
        etl = (await p.api.get("/apps/etl/eval-set")).json()
        assert (etl["source"], etl["editable"]) == ("file", False)
        refused = await p.api.put("/apps/etl/eval-set", json={"cases": []})
        assert refused.status_code == 409
        assert refused.json()["detail"] == "这个应用的评测集在仓库里维护，页面上不能改"
        assert (await p.api.get("/apps/nope/eval-set")).status_code == 404

        # Clearing the cases lifts the gate.
        await p.api.put("/apps/support/eval-set", json={"cases": []})
        assert (await p.api.get("/apps/support")).json()["has_eval_set"] is False

        # A copied app takes the eval set along.
        await p.api.put("/apps/support/eval-set", json={"cases": [case], "threshold": 0.6})
        await p.api.post(
            "/apps", json={"app": "support-2", "title": "副本", "from": {"app": "support"}}
        )
        copied = (await p.api.get("/apps/support-2/eval-set")).json()
        assert [c["id"] for c in copied["cases"]] == ["refund"]
        assert copied["threshold"] == 0.6


# Scoring a case


async def test_checks_score_the_result():
    async def judge(answer, criteria, reference):
        return ("退货" in answer, f"{criteria}/{reference}")

    case = {
        "expect": [
            {"kind": "equals", "path": "order", "value": "SO-1"},
            {"kind": "equals", "path": "n.total", "value": 3},
            {"kind": "contains", "path": "answer", "values": ["退货", "七天|7 天", "到账"]},
            {"kind": "excludes", "path": "answer", "values": ["保证", "包退"]},
            {"kind": "judge", "path": "answer", "criteria": "礼貌", "reference": "参考"},
            {"kind": "equals", "path": "missing", "value": 1},
        ]
    }
    run = {
        "status": "done",
        "result": {"order": "SO-1", "n": {"total": 4}, "answer": "退货 7 天到账，保证满意"},
    }
    checks = await eval_sets.score_flow_case(case, run, judge)
    assert [(c.layer, c.name, c.passed, c.detail) for c in checks] == [
        ("completed", "运行完成", True, ""),
        ("equals", "order 等于 SO-1", True, ""),
        ("equals", "n.total 等于 3", False, "实际是 4"),
        ("contains", "answer 包含 退货、七天|7 天、到账", True, ""),
        ("excludes", "answer 不含 保证、包退", False, "出现了 保证"),
        ("judge", "模型判断 answer：礼貌", True, "礼貌/参考"),
        ("equals", "missing 等于 1", False, "结果里没有 missing"),
    ]

    failed = await eval_sets.score_flow_case(case, {"status": "error", "error": "炸了"}, judge)
    assert [(c.passed, c.detail) for c in failed[:2]] == [(False, "炸了"), (False, "运行没有完成")]
    assert len(failed) == 7

    assert eval_sets.suggested_checks({"order": "SO-1", "answer": LONG, "n": 2}) == [
        {"kind": "equals", "path": "order", "value": "SO-1"},
        {"kind": "judge", "path": "answer", "criteria": "意思和参考答案一致", "reference": LONG},
        {"kind": "equals", "path": "n", "value": 2},
    ]
    assert eval_sets.suggested_checks("就一句") == [
        {"kind": "equals", "path": None, "value": "就一句"}
    ]
    # What a model wrote gets a judge however short; an empty one stays equals.
    worded = {"reply", "note", "order"}
    assert eval_sets.suggested_checks(
        {"reply": "共 1350 元。", "note": "", "n": 2}, set(), worded
    ) == [
        {
            "kind": "judge",
            "path": "reply",
            "criteria": "意思和参考答案一致",
            "reference": "共 1350 元。",
        },
        {"kind": "equals", "path": "note", "value": ""},
        {"kind": "equals", "path": "n", "value": 2},
    ]
    assert eval_sets.case_id("run_ABCdef123456", {"run-ef123456"}) == "run-ef123456-2"


# Feedback, a run saved as a case, and the gate


async def test_a_good_run_becomes_a_case_that_gates_publishing():
    judged = json.dumps({"pass": True, "reason": "意思一样"})
    replies = ["草稿一", LONG, LONG, judged]
    async with Platform(replies) as p:
        await p.new_app("support", support())
        run = await finished_run(p, answer_first="reject")
        assert run["status"] == "done"
        assert run["result"] == {"answer": LONG, "order": "SO-1"}
        assert run["feedback"] == {"good": 0, "bad": 0, "mine": None, "cases": []}

        rated = await p.api.put(
            f"/runs/{run['run_id']}/feedback", json={"rating": "good", "note": " 对的 "}
        )
        assert rated.status_code == 200, rated.text
        assert rated.json()["mine"]["note"] == "对的"
        assert (rated.json()["good"], rated.json()["bad"]) == (1, 0)
        rerated = await p.api.put(f"/runs/{run['run_id']}/feedback", json={"rating": "bad"})
        assert (rerated.json()["good"], rerated.json()["bad"]) == (0, 1)
        assert rerated.json()["mine"]["note"] is None
        undone = await p.api.delete(f"/runs/{run['run_id']}/feedback")
        assert undone.json() | {"cases": []} == {"good": 0, "bad": 0, "mine": None, "cases": []}
        bad = await p.api.put(f"/runs/{run['run_id']}/feedback", json={"rating": "meh"})
        assert bad.status_code == 422
        assert (await p.api.put("/runs/nope/feedback", json={"rating": "good"})).status_code == 404

        saved = await p.api.post(
            "/apps/support/eval-set/cases/from-run", json={"run_id": run["run_id"]}
        )
        assert saved.status_code == 201, saved.text
        case = saved.json()
        assert case["id"] == "run-" + run["run_id"].replace("-", "").replace("_", "")[-8:]
        assert case["input"] == {"question": "我要退货"}
        # The last answer of each person's step.
        assert case["answers"] == {
            "ask": {"values": {"order_id": "SO-1"}},
            "review": {"decision": "approve", "comment": None},
        }
        assert case["expect"] == [
            {
                "kind": "judge",
                "path": "answer",
                "criteria": "意思和参考答案一致",
                "reference": LONG,
            },
            {"kind": "equals", "path": "order", "value": "SO-1"},
        ]
        assert case["from_run"] == run["run_id"]
        seen = (await p.api.get(f"/runs/{run['run_id']}")).json()["feedback"]
        assert seen["cases"] == [{"app": "support", "case_id": case["id"]}]

        # Saved again: a second id; a bad rating leaves the checks to the builder.
        second = await p.api.post(
            "/apps/support/eval-set/cases/from-run",
            json={"run_id": run["run_id"], "rating": "bad"},
        )
        assert second.json()["id"] == case["id"] + "-2"
        assert second.json()["expect"] == []
        await p.api.put(
            "/apps/support/eval-set",
            json={
                "cases": [
                    c
                    for c in (await p.api.get("/apps/support/eval-set")).json()["cases"]
                    if c["id"] == case["id"]
                ]
            },
        )

        # The gate: the draft must pass the set before it is published.
        await p.api.put("/apps/support/draft", json={"config": support()})
        refused = await p.api.post("/apps/support/draft/publish", json={"note": "改"})
        assert refused.status_code == 409
        started = await p.api.post("/apps/support/draft/eval", json={})
        assert started.status_code == 202, started.text
        await p.catalog.wait_idle()
        result = (await p.api.get("/apps/support")).json()["draft"]["eval"]
        assert (result["status"], result["score"], result["case_count"]) == ("passed", 1.0, 1)
        [scored] = result["cases"]
        assert scored["title"] == "我要退货"
        assert [(c["kind"], c["passed"]) for c in scored["checks"]] == [
            ("completed", True),
            ("judge", True),
            ("equals", True),
        ]
        judge_request = p.gateway.requests[-1]["body"]
        assert judge_request["temperature"] == 0
        assert "参考答案" in judge_request["messages"][0]["content"]
        case_run = await p.service.get(scored["run_id"])
        assert case_run.record.created_by == "aep-eval"
        published = await p.api.post("/apps/support/draft/publish", json={"note": "改"})
        assert published.status_code == 200, published.text


async def test_cases_that_cannot_be_answered_fail():
    async with Platform(["回答", json.dumps({"pass": False, "reason": "不礼貌"})]) as p:
        await p.new_app("support", support())
        no_form = {"id": "no-form", "input": {"question": "退货"}}
        no_comment = {
            "id": "no-comment",
            "input": {"question": "退货"},
            "answers": {"ask": {"values": {"order_id": "SO-1"}}, "review": {"decision": "reject"}},
        }
        no_input = {"id": "no-input", "input": {}}
        judged = {
            "id": "judged",
            "input": {"question": "退货"},
            "answers": {"ask": {"values": {"order_id": "SO-1"}}},
            "expect": [{"kind": "judge", "path": "answer", "criteria": "礼貌"}],
        }
        await p.api.put(
            "/apps/support/eval-set",
            json={"cases": [no_form, no_comment, no_input, judged], "judge_model": "qwen"},
        )
        await p.api.put("/apps/support/draft", json={"config": support()})
        await p.api.post("/apps/support/draft/eval", json={})
        await p.catalog.wait_idle()
        result = (await p.api.get("/apps/support")).json()["draft"]["eval"]
        cases = {c["id"]: c for c in result["cases"]}
        assert cases["no-form"]["error"] == "用例没有给表单「ask」的值"
        assert cases["no-comment"]["error"] == "「确认吗？」选 reject 要写意见，用例里没写"
        assert cases["no-input"]["error"].startswith("输入不对：")
        assert cases["judged"]["score"] == 0.5
        assert cases["judged"]["checks"][1]["detail"] == "不礼貌"
        assert p.gateway.requests[-1]["body"]["model"] == "qwen"
        assert result["status"] == "failed"
        assert result["score"] == pytest.approx(0.5 / 4)


async def test_saving_a_run_as_a_case_needs_a_finished_run_of_the_app():
    async with Platform(["回答"]) as p:
        await p.new_app("support", support())
        await p.new_app("other", support())
        waiting = await p.run("support", {"question": "退货"})
        other = await finished_run(p)
        path = "/apps/support/eval-set/cases/from-run"
        cases = [
            ({"run_id": waiting["run_id"]}, 409, "运行结束后才能存成用例"),
            ({"run_id": "nope"}, 404, "没有找到这次运行"),
        ]
        for body, code, detail in cases:
            resp = await p.api.post(path, json=body)
            assert (resp.status_code, resp.json()["detail"]) == (code, detail)
        foreign = await p.run("other", {"question": "退货"})
        resp = await p.api.post(path, json={"run_id": foreign["run_id"]})
        assert resp.json()["detail"] == "这次运行不是应用 support 的"
        assert (
            await p.api.put(f"/runs/{waiting['run_id']}/feedback", json={"rating": "good"})
        ).json()["detail"] == "运行结束后才能评价"

        etl = await p.api.post(
            "/apps/etl/eval-set/cases/from-run", json={"run_id": other["run_id"]}
        )
        assert etl.status_code == 409

        found = await p.catalog.eval_sets.get("support")
        assert found is None
        await p.catalog.eval_sets.put(
            eval_sets.EvalSet(
                "support",
                0.8,
                cases=[{"id": f"c{i}", "input": {}} for i in range(eval_sets.MAX_CASES)],
            )
        )
        full = await p.api.post(path, json={"run_id": other["run_id"]})
        assert full.status_code == 409
        assert full.json()["detail"] == "一个评测集最多 50 条用例"


# The developer view


async def test_steps_show_their_time_and_model_use():
    calls = MemoryCallStore()
    llm.set_recorder(calls.record)
    try:
        async with Platform(["回答"]) as p:
            p.service.calls = calls
            await p.new_app("support", support())
            run = await finished_run(p)
            steps = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()["steps"]
    finally:
        llm.set_recorder(None)
    by_node = {s["node"]: s for s in steps}
    assert by_node["draft"]["usage"] == {
        "calls": 1,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "cost_usd": None,
    }
    assert by_node["ask"]["usage"] is None
    for step in steps:
        assert step["finished_at"] and step["duration_ms"] >= 0, step


async def test_a_run_is_rerun_from_a_step():
    async with Platform(["第一版", "第二版"]) as p:
        await p.new_app("support", support())
        source = await finished_run(p)
        assert source["result"]["answer"] == "第一版"

        # On the draft: the order and question carry over, the answer is made again.
        draft = support()
        node(draft, "draft")["prompt"] = "重写：{{start.question}} {{ask.order_id}}"
        await p.api.put("/apps/support/draft", json={"config": draft})
        made = await p.api.post(f"/runs/{source['run_id']}/rerun-from", json={"node": "draft"})
        assert made.status_code == 201, made.text
        await p.service.wait_idle()
        again = (await p.api.get(f"/runs/{made.json()['run_id']}")).json()
        assert again["status"] == "waiting"  # at the approval, not the order form
        assert again["pending"]["details"]["node"] == "review"
        assert again["start_at"] == {"node": "draft", "title": "回答", "run_id": source["run_id"]}
        assert (again["app_version"], again["draft"]) == (None, True)
        assert again["rerun_of"] == source["run_id"]
        assert p.gateway.requests[-1]["body"]["messages"][-1]["content"] == "重写：我要退货 SO-1"
        again = await p.resume(again, decision="approve")
        assert again["result"] == {"answer": "第二版", "order": "SO-1"}
        assert [
            s["node"] for s in (await p.api.get(f"/runs/{again['run_id']}/flow")).json()["steps"]
        ][:1] == ["draft"]

        # On the run's own version.
        same = await p.api.post(
            f"/runs/{source['run_id']}/rerun-from", json={"node": "review", "config": "same"}
        )
        await p.service.wait_idle()
        same = (await p.api.get(f"/runs/{same.json()['run_id']}")).json()
        assert same["app_version"] == source["app_version"] == 2
        assert same["pending"]["details"]["show"] == "第一版"

        wrong = [
            ({"node": "nope"}, 422, "草稿里没有节点 nope"),
            ({"node": "nope", "config": "same"}, 422, "原运行的配置里没有节点 nope"),
            ({"node": "start"}, 422, "从开始节点重跑就是重新运行，请用“复制并重新发起”"),
        ]
        for body, code, detail in wrong:
            resp = await p.api.post(f"/runs/{source['run_id']}/rerun-from", json=body)
            assert (resp.status_code, resp.json()["detail"]) == (code, detail), body
        early = await p.run("support", {"question": "退货"})  # waits at the order form
        resp = await p.api.post(f"/runs/{early['run_id']}/rerun-from", json={"node": "review"})
        assert resp.json()["detail"] == "原运行没有跑到「确认」这一步"
        assert (
            await p.api.post("/runs/nope/rerun-from", json={"node": "draft"})
        ).status_code == 404

        hello = await p.run("hello", {"text": "你好"})
        resp = await p.api.post(
            f"/runs/{hello['run_id']}/rerun-from", json={"node": "draft", "config": "draft"}
        )
        assert (resp.status_code, resp.json()["detail"]) == (409, "这个应用没有草稿")

        await p.api.delete("/apps/support/draft")
        resp = await p.api.post(f"/runs/{source['run_id']}/rerun-from", json={"node": "draft"})
        assert resp.status_code == 409


async def test_code_apps_cannot_rerun_from_a_step():
    async with Platform(["x"]) as p:
        record = await p.service.create("etl", {"request": "x", "datasource_id": "nope"})
        await p.service.wait_idle()
        resp = await p.api.post(f"/runs/{record.run_id}/rerun-from", json={"node": "plan"})
        assert (resp.status_code, resp.json()["detail"]) == (409, "代码应用不能从某一步重跑")
