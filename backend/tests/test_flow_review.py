"""The node llm_review (docs/api/flow-apps.md, "llm_review 模型审查"): another model call
reviews what an earlier step produced against a checklist (jadeflow 4th retest I-12,
I-18)."""

import json

import pytest

from aep.runtime.flow.check import ConfigError, check_config
from aep.runtime.flow.nodes import TYPES, CheckEnv, NodeFailure, Runtime
from aep.runtime.flow.review import no_change, readable, rules_of
from aep.skills import MemorySkillStore
from tests.conftest import FakeGateway

PROJECT = {
    "conventions": [
        {"key": "by_version", "title": "读 table1 先按版本筛选", "text": "", "applies_to": "code"},
        {"key": "ask_open", "title": "没定的事要提问", "text": "资料说没定的"}
        | {"applies_to": "plan"},
        {"key": "names", "title": "列名用小写", "text": "", "applies_to": "both"},
    ]
}


def review_node(**changes) -> dict:
    return {
        "id": "review_plan",
        "type": "llm_review",
        "title": "审查方案",
        "subject": "{{check_plan.plan}}",
        "context": "需求：{{start.request}}",
        "rules": [
            {"key": "open_items", "title": "资料写明还没定的事要写成问题", "instruction": "看资料"},
            "{{project.plan_rules}}",
        ],
        "max_attempts": 2,
        "on_stuck": "pass",
    } | changes


def state(private: dict | None = None, project: dict | None = PROJECT, decisions: str = "") -> dict:
    return {
        "input": {},
        "vars": {
            "start": {"request": "负数按 0"},
            "check_plan": {"plan": {"rules": [{"id": "r5", "text": "负数按 0"}]}},
            "review": {"decisions": decisions},
        },
        "run_id": "run-1",
        "project": project,
        "private": {"review_plan": private} if private else {},
    }


def finding(rule: str = "open_items", **changes) -> dict:
    return {
        "rule": rule,
        "where": "规则 r5",
        "problem": "资料说负数按 0 还没定，方案直接定了",
        "quote": "Q-081 还没定",
        "fix": "写成问题",
    } | changes


def reply(*found: dict) -> str:
    return json.dumps({"findings": list(found)}, ensure_ascii=False)


def runtime(*replies: str) -> tuple[Runtime, FakeGateway]:
    gateway = FakeGateway(list(replies))
    return Runtime(gateway.client, "mock", {}, {}), gateway


async def run(node: dict, st: dict, rt: Runtime):
    return await TYPES["llm_review"].run(node, st, rt)


def test_the_checklist_takes_the_nodes_rules_and_the_ones_it_refers_to():
    lookup = Runtime(FakeGateway().client, "mock", {}, {}).lookup(state())
    rules = rules_of(review_node(), lookup)
    assert [(r["key"], r["title"]) for r in rules] == [
        ("open_items", "资料写明还没定的事要写成问题"),
        ("ask_open", "没定的事要提问"),
        ("names", "列名用小写"),
    ]
    # A repeated key or text gets a key of its own; a run without a project has none.
    node = review_node(rules=[{"key": "a", "title": "一"}, {"key": "a", "title": "二"}])
    assert [r["key"] for r in rules_of(node, lookup)] == ["a", "rule_2"]
    unbound = Runtime(FakeGateway().client, "mock", {}, {}).lookup(state(project=None))
    assert [r["key"] for r in rules_of(review_node(), unbound)] == ["open_items"]


def test_what_is_reviewed_is_laid_out_for_reading():
    assert readable({"t1": "rows = source('a')\nreturn rows"}) == (
        "### t1\n\nrows = source('a')\nreturn rows"
    )
    assert readable({"rules": [1]}) == '{\n  "rules": [\n    1\n  ]\n}'
    assert readable("文字") == "文字" and readable(None) == ""


async def test_a_review_without_problems_goes_on():
    rt, gateway = runtime(reply())
    out = await run(review_node(), state(), rt)
    assert out.branch == "ok"
    assert out.outputs == {
        "ok": True,
        "findings": [],
        "problems": "",
        "report": "",
        "summary": "审查方案：按 3 条清单审查，没有发现问题",
    }
    # The reviewer is asked about the checklist only, with the material and the plan.
    [request] = gateway.requests
    system, user = (m["content"] for m in request["body"]["messages"])
    assert "只按「审查清单」逐条检查" in system
    assert user.startswith("审查清单：\n- 〔open_items〕资料写明还没定的事要写成问题：看资料\n")
    assert "- 〔ask_open〕没定的事要提问：资料说没定的" in user
    assert "参考资料：\n需求：负数按 0" in user
    assert '要审查的内容：\n{\n  "rules": [\n    {\n      "id": "r5",' in user


async def test_problems_go_back_and_after_the_last_attempt_the_person_sees_them():
    rt, gateway = runtime(reply(finding()), reply(finding()))
    out = await run(review_node(), state(), rt)
    assert out.branch == "retry" and out.private == {"failures": 1}
    assert out.outputs["ok"] is False and out.outputs["report"] == ""
    assert out.outputs["problems"] == (
        "- 〔资料写明还没定的事要写成问题〕（规则 r5）资料说负数按 0 还没定，方案直接定了"
        " 怎么改：写成问题（依据：Q-081 还没定）"
    )
    assert out.outputs["findings"][0]["title"] == "资料写明还没定的事要写成问题"
    assert out.messages[0]["content"] == "审查方案发现 1 处问题，让上一步带着问题重写（第 1/2 次）"

    # Not fixed the second time: the run goes on, the person who confirms next sees them,
    # and a rewrite for that person's comment is not sent back to them.
    out = await run(review_node(), state(private=out.private), rt)
    assert out.branch == "ok" and out.private == {"failures": 0}
    assert out.outputs["ok"] is False and out.outputs["problems"] == ""
    report = out.outputs["report"]
    assert report.startswith("### 审查方案：没改过来的问题\n\n按审查清单连续审查了 2 次")
    assert "- **资料写明还没定的事要写成问题**（规则 r5）：资料说负数按 0 还没定" in report
    assert out.messages[0]["content"] == "审查方案连续 2 次发现问题，没改过来，交给后面确认的人看"


async def test_a_review_that_cannot_go_on_hands_over_or_fails():
    rt, _ = runtime(reply(finding()))
    node = review_node(max_attempts=1, on_stuck="branch")
    assert TYPES["llm_review"].branches(node) == ["ok", "retry", "stuck"]
    out = await run(node, state(), rt)
    assert out.branch == "stuck" and out.outputs["problems"]
    assert out.outputs["report"].startswith("### 审查方案：没改过来的问题")
    with pytest.raises(NodeFailure, match="审查方案连续 1 次发现问题"):
        await run(review_node(max_attempts=1, on_stuck="fail"), state(), rt)


async def test_no_checklist_no_model_call():
    rt, gateway = runtime(reply(finding()))
    node = review_node(rules=["{{project.code_rules}}"])
    out = await run(node, state(project={"conventions": []}), rt)
    assert out.branch == "ok" and out.outputs["ok"] is True
    assert out.outputs["summary"] == "审查方案：没有审查清单，跳过"
    assert gateway.requests == []


async def test_a_reply_off_the_checklist_is_asked_again():
    rt, gateway = runtime(reply(finding(rule="made_up")), reply())
    out = await run(review_node(), state(), rt)
    assert out.branch == "ok" and len(gateway.requests) == 2
    again = gateway.requests[1]["body"]["messages"][-1]["content"]
    assert "rule 只能是清单里的标识：open_items、ask_open、names" in again


def test_a_finding_that_needs_no_change_is_none():
    # jadeflow 5th retest I-21: reviewers listed points and wrote they need no change.
    assert not no_change(finding())
    assert no_change(finding(violates=False))
    assert no_change(finding(fix="")) and no_change(finding(fix=None))
    for problem in (
        "当前代码用 v 汇总，符合要求，但需确认",
        "此处判定 q3 已覆盖，不记",
        "该点不构成违反",
        "此处无违规，仅确认读法正确",
    ):
        assert no_change(finding(problem=problem)), problem
    assert no_change(finding(fix="无需修改"))
    # Saying something does not meet a rule, or about a log record, is a finding.
    assert not no_change(finding(problem="规则 r5 不符合要求：负数按 0 还没定"))
    assert not no_change(finding(problem="出错时不记录日志", fix="加一句日志"))


async def test_findings_that_need_no_change_are_dropped_and_not_counted():
    rt, gateway = runtime(
        reply(finding(violates=False), finding(fix=""), finding(problem="符合约定，不记问题")),
        reply(finding(problem="此处正确……这条不构成违规"), finding()),
    )
    out = await run(review_node(), state(), rt)
    assert out.branch == "ok" and out.outputs["ok"] is True and out.outputs["findings"] == []
    assert out.outputs["summary"] == (
        "审查方案：按 3 条清单审查，没有发现问题（另有 3 条写着不用改，已忽略）"
    )
    out = await run(review_node(), state(), rt)
    assert out.branch == "retry" and len(out.outputs["findings"]) == 1
    assert out.messages[0]["content"] == (
        "审查方案发现 1 处问题，让上一步带着问题重写（第 1/2 次）（另有 1 条写着不用改，已忽略）"
    )
    system = gateway.requests[0]["body"]["messages"][0]["content"]
    assert '"violates": true' in system and "不要写「无需修改」「不记」「符合要求」" in system


async def test_what_people_decided_is_kept_and_not_reported():
    # jadeflow 5th retest I-20: the reviewer had answered questions asked again; I-21: it
    # suggested removing what a person asked for. Decisions come first in the checklist.
    decisions = (
        "【第 1 轮 · 确认方案 · 修改】「负数的计费重量怎么处理？」选「按资料，原样保留」"
        "（原建议「按需求，负数按 0」）"
    )
    rt, gateway = runtime(reply())
    node = review_node(decided="{{review.decisions}}")
    out = await run(node, state(decisions=decisions), rt)
    assert out.outputs["summary"] == "审查方案：按 4 条清单审查，没有发现问题"
    system, user = (m["content"] for m in gateway.requests[0]["body"]["messages"])
    assert "按这些决定写的内容不是问题：不要报成违反，也不要建议撤掉或改掉它们。" in system
    assert user.startswith(
        "审查清单：\n- 〔decided〕人已经做的决定都要做到：「人已经做的决定」里的每一条"
    )
    assert (
        "人已经做的决定（按时间先后，同一件事以后面的为准）：\n" + decisions + "\n\n要审查的内容："
    ) in user

    # Without conventions there is still something to review: what people asked for.
    rt, gateway = runtime(reply(finding(rule="decided")))
    node = review_node(rules=["{{project.code_rules}}"], decided="{{review.decisions}}")
    out = await run(node, state(project={"conventions": []}, decisions=decisions), rt)
    assert out.branch == "retry" and out.outputs["findings"][0]["title"] == "人已经做的决定都要做到"
    # Nothing decided yet: no such rule, and no model call without other rules.
    rt, gateway = runtime(reply())
    out = await run(node, state(project={"conventions": []}), rt)
    assert out.outputs["summary"] == "审查方案：没有审查清单，跳过" and gateway.requests == []


async def test_settings_are_checked():
    env = CheckEnv(models=None, skills=MemorySkillStore(), strict=False)

    def config(**changes) -> dict:
        return {
            "env": {},
            "graph": {
                "nodes": [
                    {
                        "id": "start",
                        "type": "start",
                        "title": "开始",
                        "inputs": [{"key": "reply", "label": "回复", "type": "text"}],
                    },
                    {
                        "id": "review",
                        "type": "llm_review",
                        "title": "审查回复",
                        "subject": "{{start.reply}}",
                        "rules": [{"key": "a", "title": "不能承诺退款时间"}],
                    }
                    | changes,
                    {
                        "id": "end",
                        "type": "end",
                        "title": "结束",
                        "outputs": [{"key": "out", "from": ["{{review.summary}}"]}],
                    },
                ],
                "edges": [
                    {"from": "start", "to": "review"},
                    {"from": "review", "to": "end", "branch": "ok"},
                    {"from": "review", "to": "end", "branch": "retry"},
                ],
            },
        }

    await check_config(config(), env)
    bad = config(
        subject="",
        rules=[{"key": "A", "title": ""}, "没有引用的文字"],
        max_attempts=9,
        on_stuck="sometimes",
    )
    with pytest.raises(ConfigError) as failed:
        await check_config(bad, env)
    found = {tuple(loc[4:]): message for loc, message in failed.value.problems}
    assert found[("subject",)] == "要审查的内容不能为空"
    assert found[("rules", 0, "key")].startswith("标识只能用小写字母")
    assert found[("rules", 0, "title")] == "规则名称不能为空"
    assert found[("rules", 1)] == "清单里的文字要引用一组规则，比如 {{project.code_rules}}"
    assert found[("max_attempts",)] == "连续几次没改过来 max_attempts 要在 1–5 之间"
    assert found[("on_stuck",)].startswith("没改过来之后 on_stuck 只能是 pass")
    with pytest.raises(ConfigError) as failed:
        await check_config(config(rules=[]), env)
    assert any(m == "审查清单要有 1–20 条" for _, m in failed.value.problems)
