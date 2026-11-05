"""Projects: the API, binding runs to them, {{param.*}} and {{project.*}}
(docs/api/projects.md)."""

import copy
import json
import time
from typing import Any

import pytest

from aep.config import get_settings
from aep.main import app
from aep.projects import (
    BindError,
    MemoryProjectStore,
    ProjectBody,
    filter_problem,
    param_problem,
    pattern_problem,
    snapshot,
)
from tests.conftest import SETTINGS
from tests.test_flow_tools import Platform

PROJECT: dict[str, Any] = {
    "project_id": "jade-ibf",
    "name": "IBF 入库运费",
    "description": "Table 2 预测",
    "table_prefix": "ibf_",
    "environments": [
        {"key": "dev", "label": "开发", "catalog": "icsd-ace-dev", "schema": "jade_dev"},
        {
            "key": "prod",
            "label": "生产",
            "catalog": "icsd-ace-prod",
            "schema": "jade",
            "cluster_id": "c-9",
        },
    ],
    "references": [
        {
            "name": "prev.sales.forecast",
            "label": "预测表",
            "filter": "Type = 'Vol.'",
            "description": "上一期的需求预测",
        }
    ],
    "adopted": ["ibf_table1_cw"],
    "params": [
        {
            "key": "update_month",
            "label": "统计月",
            "type": "month",
            "required": True,
            "default": "2026-10",
            "description": "按自然月",
        },
        {"key": "version", "label": "版本", "type": "number"},
    ],
    "policy": {"samples": None, "models": None},
}


def project(**changes: Any) -> dict[str, Any]:
    return copy.deepcopy(PROJECT) | changes


# Rules


def test_read_filters_are_checked_for_shape_only():
    assert filter_problem("Type = 'Vol.' AND (qty > 0)") is None
    assert filter_problem("note = 'a;b -- (select'") is None  # Inside a literal.
    assert filter_problem("qty > 0; DROP TABLE x") == "读取筛选不能有分号"
    assert filter_problem("qty > 0 -- x") == "读取筛选不能有注释"
    assert filter_problem("qty > 0 /* x */") == "读取筛选不能有注释"
    assert filter_problem("id IN (SELECT id FROM t)") == "读取筛选不能有 SELECT"
    assert filter_problem("(qty > 0") == "读取筛选的括号要配对"
    assert filter_problem("qty > 0)") == "读取筛选的括号要配对"
    assert filter_problem("Type = 'Vol.") == "读取筛选的引号要配对"
    assert filter_problem("selected = true") is None  # Not the keyword.


def test_parameter_values_follow_their_type():
    month = {"label": "统计月", "type": "month"}
    assert param_problem(month, "2026-09") is None
    assert param_problem(month, "2026-13") == "参数「统计月」要写成 YYYY-MM"
    assert param_problem({"label": "日", "type": "date"}, "2026-02-30") == (
        "参数「日」要写成 YYYY-MM-DD"
    )
    assert param_problem({"label": "数", "type": "number"}, True) == "参数「数」要是数字"
    assert param_problem({"label": "开关", "type": "boolean"}, "true") == (
        "参数「开关」要是 true 或 false"
    )
    assert param_problem({"label": "字", "type": "text"}, "") == (
        "参数「字」要写 1–500 个字，不能只有空格"
    )
    assert param_problem({"label": "字", "type": "text"}, "   ") == (
        "参数「字」要写 1–500 个字，不能只有空格"
    )
    choice = {"label": "版本", "type": "select", "choices": [{"value": "a"}, {"value": "b"}]}
    assert param_problem(choice, "c") == "参数「版本」要是这些之一：a、b"


def test_a_text_parameter_may_have_a_format():
    version = {"label": "版本", "type": "text", "pattern": r"FY\d{2}_\d{2}_M"}
    assert param_problem(version, "FY26_06_M") is None
    # The whole value must match.
    for value in ("FY26_06_MX", "xFY26_06_M", "FY26-06"):
        assert param_problem(version, value) == (
            r"参数「版本」的格式不对，要整段符合正则表达式 FY\d{2}_\d{2}_M"
        )
    # With a description, people read that instead of the expression (jadeflow retest S-19).
    described = version | {"description": "月度版本 FY{yy}_{MM}_M"}
    assert param_problem(described, "FY26") == "参数「版本」的格式不对：月度版本 FY{yy}_{MM}_M"
    # A hint on the format is told instead of the whole description (4th retest).
    hinted = described | {"description": "月度版本；没有默认值，每次都要填",
                          "pattern_hint": "FY 两位年份_两位月份_M，如 FY26_07_M"}  # fmt: skip
    assert param_problem(hinted, "FY26") == (
        "参数「版本」的格式不对：FY 两位年份_两位月份_M，如 FY26_07_M"
    )
    with pytest.raises(ValueError, match="参数「字」没有写格式，不用写格式说明"):
        ProjectBody.model_validate(
            project(params=[{"key": "w", "label": "字", "type": "text", "pattern_hint": "x"}])
        )
    assert param_problem({"label": "字", "type": "text"}, "x") is None  # Saved before formats.
    assert pattern_problem("(a") == "不是有效的正则表达式（missing ): (a）"
    assert pattern_problem(r"(a)\1") == r"不是有效的正则表达式（invalid escape sequence: \1）"
    # RE2 matches in linear time: Python's re would backtrack on this for minutes.
    started = time.monotonic()
    slow = {"label": "字", "type": "text", "pattern": r"(\w+)+\w*\w*\w*x"}
    assert param_problem(slow, "a" * 500) is not None
    assert time.monotonic() - started < 1


# The API


async def test_project_crud():
    async with Platform([]) as p:
        made = await p.api.post("/projects", json=project())
        assert made.status_code == 201, made.text
        body = made.json()
        assert body["environments"][0]["cluster_id"] is None
        assert body["params"][1]["required"] is False
        assert body["created_at"] and body["updated_at"]

        listed = (await p.api.get("/projects")).json()
        assert listed == [
            {
                "project_id": "jade-ibf",
                "name": "IBF 入库运费",
                "description": "Table 2 预测",
                "environments": [{"key": "dev", "label": "开发"}, {"key": "prod", "label": "生产"}],
                "updated_at": body["updated_at"],
            }
        ]
        again = await p.api.post("/projects", json=project())
        assert again.status_code == 409
        assert again.json()["detail"] == "项目 jade-ibf 已经存在"
        same_name = await p.api.post("/projects", json=project(project_id="other"))
        assert same_name.json()["detail"] == "已经有叫「IBF 入库运费」的项目"

        changed = await p.api.put("/projects/jade-ibf", json=project(name="IBF 运费"))
        assert changed.status_code == 200, changed.text
        assert (await p.api.get("/projects/jade-ibf")).json()["name"] == "IBF 运费"
        moved = await p.api.put("/projects/jade-ibf", json=project(project_id="x-y"))
        assert moved.status_code == 422
        missing = await p.api.put("/projects/nope", json=project(project_id="nope"))
        assert missing.status_code == 404

        assert (await p.api.delete("/projects/jade-ibf")).status_code == 204
        gone = await p.api.get("/projects/jade-ibf")
        assert gone.status_code == 404 and gone.json()["detail"] == "没有项目 jade-ibf"
        assert (await p.api.delete("/projects/jade-ibf")).status_code == 404


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"project_id": "Jade"}, "「项目标识」的格式、长度或范围不正确"),
        ({"project_id": "new"}, "项目标识不能是 new，页面地址 /projects/new 用了它"),
        ({"adopted": ["table1"]}, "归本项目的表 table1 要以表名前缀 ibf_ 开头"),
        ({"repository": "x"}, "项目不接受「repository」这个设置"),
        ({"environments": []}, "「环境」的格式、长度或范围不正确"),
        (
            {"references": [{"name": "a.b", "label": "x"}]},
            "引用表 a.b 要写成 catalog.schema.table 三段，每段不含空白和反引号",
        ),
        (
            {"references": [{"name": "a.b.c", "filter": "1=1; drop"}]},
            "引用表 a.b.c：读取筛选不能有分号",
        ),
        (
            {"params": [{"key": "m", "label": "统计月", "type": "month", "default": "10月"}]},
            "默认值不对：参数「统计月」要写成 YYYY-MM",
        ),
        (
            {"params": [{"key": "v", "label": "版本", "type": "select"}]},
            "参数「版本」是选项，要列出 1–50 个选项",
        ),
        (
            {"params": [{"key": "m", "label": "统计月", "type": "month", "pattern": "x"}]},
            "参数「统计月」不是文本，不能写格式",
        ),
        (
            {"params": [{"key": "v", "label": "版本", "type": "text", "pattern": "(?<=a)b"}]},
            "参数「版本」的格式不是有效的正则表达式（invalid perl operator: (?<=）",
        ),
        (
            {
                "params": [
                    {
                        "key": "v",
                        "label": "版本",
                        "type": "text",
                        "pattern": "FY\\d{2}",
                        "default": "FY2",
                    }
                ]
            },
            "默认值不对：参数「版本」的格式不对，要整段符合正则表达式 FY\\d{2}",
        ),
        ({"policy": {"samples": "some"}}, "「数据外发策略的样本级别」的取值不对"),
    ],
)
async def test_project_rules(changes, detail):
    async with Platform([]) as p:
        resp = await p.api.post("/projects", json=project(**changes))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == detail


TOTAL = {
    "id": "c1_rows",
    "label": "本月有结果",
    "kind": "aggregate",
    "table": "ibf_table2",
    "expr": "count(*)",
    "where": "update_month = ${param.update_month}",
    "op": "gt",
    "value": 0,
}
OLD = {
    "id": "c6_old",
    "label": "和老系统一致",
    "kind": "golden",
    "severity": "warn",
    "table": "ibf_table2",
    "expected": "old.jade.table2",
    "keys": ["sku"],
    "expected_where": "version = ${param.version}",
    "tolerance": 0.001,
}


@pytest.mark.parametrize(
    ("checks", "detail"),
    [
        ([{**TOTAL, "op": "between"}], "验收检查 c1_rows（value）：between 要写数字 value 和 max"),
        (
            [{**TOTAL, "id": "C1"}],
            "验收检查 C1（id）：检查 id 只能用小写字母、数字和下划线，以字母开头，最多 32 个字符",
        ),  # fmt: skip
        (["x"], "第 1 条验收检查：每条检查要写成一个对象"),
        (
            [{**TOTAL, "id": "golden"}],
            "验收检查的 id 不能用 golden，它留给数据检查节点和老系统的对比",
        ),
        ([TOTAL, {**OLD, "id": "c1_rows"}], "验收检查 id c1_rows 重复"),
        (
            [{**OLD, "expected_where": "version = ${param.month}"}],
            "验收检查 c6_old（expected_where）用的运行参数 ${param.month} 不是本项目定义的参数",
        ),
        (
            [{**TOTAL, "table": "table2"}],
            "验收检查 c1_rows（table）：不带 catalog、schema 的 table2 是本项目在运行环境里的表，"
            "要以表名前缀 ibf_ 开头",
        ),
        ([TOTAL] * 51, "「验收检查」的格式、长度或范围不正确"),
    ],
)
async def test_acceptance_checks_are_checked(checks, detail):
    async with Platform([]) as p:
        resp = await p.api.post("/projects", json=project(checks=checks))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == detail


async def test_acceptance_checks_are_kept_and_offered_to_runs():
    async with Platform(["好的"]) as p:
        made = await p.api.post("/projects", json=project(checks=[TOTAL, OLD]))
        assert made.status_code == 201, made.text
        assert made.json()["checks"] == [TOTAL, OLD]
        # Projects saved before checks existed read as having none.
        assert (await p.api.post("/projects", json={
            k: v for k, v in project(project_id="old-one", name="旧项目").items() if k != "checks"
        })).json()["checks"] == []  # fmt: skip

        flow = bound_flow(prompt="{{project.brief}}\n检查：{{project.checks}}")
        await p.new_app("bound", flow)
        run = await start_run(p, "bound", {"project_id": "jade-ibf"})
        assert run.status_code == 201, run.text
        await p.service.wait_idle()
        sent = p.gateway.requests[0]["body"]["messages"][-1]["content"]
        assert "### 验收检查（项目已经写好，每次数据检查都会跑）" in sent
        assert (
            "| `c6_old` | 和老系统一致 | warn | `ibf_table2` 和老系统的 `old.jade.table2`"
            "按 `sku` 逐行一致 |" in sent
        )
        assert "| `c1_rows` | 本月有结果 | error |" in sent
        assert "方案里建议的数据检查不要重复这些，id 也不能和它们相同。" in sent
        assert '"id": "c1_rows"' in sent.split("检查：", 1)[1]


def test_project_checks_in_old_snapshots_and_unbound_runs():
    from aep.projects import brief, project_value

    snap = {
        "name": "x", "description": "", "table_prefix": "ibf_", "references": [], "params": [],
        "values": {}, "environment": {"key": "dev", "label": "开发", "catalog": "c", "schema": "s"},
    }  # fmt: skip
    assert project_value(snap, "checks") == []
    assert "验收检查" not in brief(snap)
    assert project_value(None, "checks") is None
    # A text parameter's format is in the brief.
    params = [{"key": "version_id", "label": "版本", "type": "text", "pattern": r"FY\d{2}|x"}]
    said = brief(snap | {"params": params, "values": {"version_id": "x"}})
    assert r"| `version_id` | 版本 | text，格式 `FY\d{2}\|x` | x | — |" in said


CONVENTIONS = [
    {
        "key": "by_version",
        "title": "读 table1_cw 先按版本筛选",
        "text": "所有读 table1_cw 的地方先按 version_id 筛选",
        "applies_to": "code",
    },
    {"key": "ask_open", "title": "资料没定的事要提问", "text": "", "applies_to": "plan"},
    {"key": "lower", "title": "列名用小写", "text": "", "applies_to": "both"},
]


async def test_conventions_are_told_to_the_model_and_reviewed_against():
    """jadeflow 4th retest I-18: a project's conventions go into the brief the model that
    writes the plan or code reads, and into the checklist of a review node."""
    async with Platform(["好的"]) as p:
        repeated = [CONVENTIONS[0], CONVENTIONS[0]]
        resp = await p.api.post("/projects", json=project(conventions=repeated))
        assert resp.json()["detail"] == "约定 by_version 重复"
        wrong = [{**CONVENTIONS[0], "applies_to": "deploy"}]
        resp = await p.api.post("/projects", json=project(conventions=wrong))
        assert resp.status_code == 422, resp.text
        made = await p.api.post("/projects", json=project(conventions=CONVENTIONS))
        assert made.status_code == 201, made.text
        assert made.json()["conventions"] == CONVENTIONS

        flow = bound_flow(prompt="{{project.brief}}\n代码：{{project.code_rules}}")
        await p.new_app("bound", flow)
        run = await start_run(p, "bound", {"project_id": "jade-ibf"})
        assert run.status_code == 201, run.text
        await p.service.wait_idle()
        sent = p.gateway.requests[0]["body"]["messages"][-1]["content"]
        said, rules = sent.split("代码：", 1)
        assert "### 约定（必须遵守，平台会按它们审查）" in said
        assert (
            "- **读 table1_cw 先按版本筛选**（写代码时）："
            "所有读 table1_cw 的地方先按 version_id 筛选" in said
        )
        assert "- **资料没定的事要提问**（写方案时）" in said
        assert "- **列名用小写**（写方案和写代码时）" in said
        assert [r["key"] for r in json.loads(rules)] == ["by_version", "lower"]


def test_conventions_in_old_snapshots_and_unbound_runs():
    from aep.projects import brief, project_value

    snap = {
        "name": "x", "description": "", "table_prefix": "ibf_", "references": [], "params": [],
        "values": {}, "environment": {"key": "dev", "label": "开发", "catalog": "c", "schema": "s"},
    }  # fmt: skip
    for name in ("conventions", "plan_rules", "code_rules"):
        assert project_value(snap, name) == [] and project_value(None, name) == []
    assert "约定" not in brief(snap)
    rules = project_value(snap | {"conventions": CONVENTIONS}, "plan_rules")
    assert rules == [
        {"key": "ask_open", "title": "资料没定的事要提问", "instruction": ""},
        {"key": "lower", "title": "列名用小写", "instruction": ""},
    ]


async def test_a_cap_on_the_checks_the_model_proposes():
    """jadeflow 4th retest I-10: a project caps how severe a check the model proposes may
    be; it is kept, in the snapshot, and told to the model in the brief."""
    async with Platform(["好的"]) as p:
        resp = await p.api.post("/projects", json=project(model_check_max_severity="fatal"))
        assert resp.status_code == 422, resp.text
        made = await p.api.post("/projects", json=project())
        assert made.json()["model_check_max_severity"] == "error"
        changed = await p.api.put(
            "/projects/jade-ibf", json=project(model_check_max_severity="warn")
        )
        assert changed.json()["model_check_max_severity"] == "warn", changed.text

        flow = bound_flow(prompt="{{project.brief}}")
        await p.new_app("bound", flow)
        run = await start_run(p, "bound", {"project_id": "jade-ibf"})
        assert run.status_code == 201, run.text
        await p.service.wait_idle()
        said = p.gateway.requests[0]["body"]["messages"][-1]["content"]
        # The brief is written from the run's snapshot.
        assert "方案里建议的数据检查最高只能是 warn（项目规定），写得更高的按 warn 运行。" in said


def test_the_cap_in_old_snapshots():
    from aep.projects import brief, project_value

    snap = {
        "name": "x", "description": "", "table_prefix": "ibf_", "references": [], "params": [],
        "values": {}, "environment": {"key": "dev", "label": "开发", "catalog": "c", "schema": "s"},
    }  # fmt: skip
    assert "最高只能是" not in brief(snap)
    assert "最高只能是" not in brief(snap | {"model_check_max_severity": "error"})
    assert "最高只能是 info" in brief(snap | {"model_check_max_severity": "info"})
    # The page reads it too (jadeflow 5th retest S-24); old snapshots allow errors.
    assert project_value(snap, "model_check_max_severity") == "error"
    capped = snap | {"model_check_max_severity": "warn"}
    assert project_value(capped, "model_check_max_severity") == "warn"
    assert project_value(None, "model_check_max_severity") is None


async def test_repeated_keys_and_environment_fields():
    async with Platform([]) as p:
        envs = [PROJECT["environments"][0], PROJECT["environments"][0]]
        resp = await p.api.post("/projects", json=project(environments=envs))
        assert resp.json()["detail"] == "环境 dev 重复"
        bad = [{**PROJECT["environments"][0], "key": "Dev"}]
        resp = await p.api.post("/projects", json=project(environments=bad))
        assert resp.json()["detail"] == "「环境的第 1 项的标识」的格式、长度或范围不正确"


async def test_options_and_models_on_the_gateway():
    settings = SETTINGS.model_copy(update={"data_samples": "stats"})
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        await _options(settings)
    finally:
        app.dependency_overrides.pop(get_settings)


async def _options(settings: Any) -> None:
    async with Platform([], settings=settings) as p:
        options = (await p.api.get("/projects/options")).json()
        assert [level["value"] for level in options["sample_levels"]] == [
            "full",
            "masked",
            "stats",
            "schema",
        ]
        assert options["sample_levels"][2]["label"] == "只发统计"
        assert options["models"] == ["mock", "qwen"]
        assert options["defaults"] == {"samples": "stats", "models": None}
        resp = await p.api.post(
            "/projects", json=project(policy={"samples": None, "models": ["deepseek"]})
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "模型网关上没有模型 deepseek"
        resp = await p.api.post(
            "/projects", json=project(policy={"samples": "masked", "models": ["qwen"]})
        )
        assert resp.status_code == 201, resp.text


# Runs that bind a project


def bound_flow(uses: str | None = "required", prompt: str | None = None) -> dict:
    start: dict[str, Any] = {
        "id": "start",
        "type": "start",
        "title": "开始",
        "inputs": [{"key": "question", "label": "问题", "type": "text", "required": True}],
    }
    if uses:
        start["project"] = uses
    ask = {
        "id": "ask",
        "type": "llm",
        "title": "回答",
        "prompt": prompt
        or "统计月 {{param.update_month}}，写到 {{project.catalog}}.{{project.schema}}\n"
        "{{project.brief}}\n{{start.question}}",
    }
    return {
        "graph": {
            "nodes": [
                start,
                ask,
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "answer", "from": ["{{ask.text}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
        }
    }


async def start_run(p: Platform, app: str, choice: dict | None, **extra: Any) -> Any:
    body: dict[str, Any] = {"app": app, "input": {"question": "多少？"}, **extra}
    if choice is not None:
        body["project"] = choice
    return await p.api.post("/runs", json=body)


async def test_a_run_binds_a_project_and_keeps_a_snapshot():
    async with Platform(["好的", "再来"]) as p:
        await p.api.post("/projects", json=project())
        await p.new_app("bound", bound_flow())
        detail = (await p.api.get("/apps/bound")).json()
        assert detail["project"] == "required"
        assert (await p.api.get("/apps/hello")).json()["project"] is None

        made = await start_run(p, "bound", {"project_id": "jade-ibf", "params": {"version": 3}})
        assert made.status_code == 201, made.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{made.json()['run_id']}")).json()
        assert run["status"] == "done", run["error"]
        assert run["project"] == {
            "project_id": "jade-ibf",
            "name": "IBF 入库运费",
            "environment": "dev",
            "environment_label": "开发",
            "params": {"update_month": "2026-10", "version": 3},
            "param_labels": {"update_month": "统计月", "version": "版本"},
            "policy": {"samples": None, "models": None},
        }
        assert run["policy"] == {"samples": "full", "models": None}
        sent = p.gateway.requests[0]["body"]["messages"][-1]["content"]
        assert sent.startswith("统计月 2026-10，写到 icsd-ace-dev.jade_dev\n## 项目：IBF 入库运费")
        assert "| `prev.sales.forecast` | 预测表 | Type = 'Vol.' | 上一期的需求预测 |" in sent
        assert "| `update_month` | 统计月 | month | 2026-10 | 按自然月 |" in sent
        assert '用 `param("标识")` 读参数' in sent
        listed = (await p.api.get("/runs")).json()
        assert listed[0]["project"]["project_id"] == "jade-ibf"

        # Changing the project does not change the run; a rerun binds what it is now.
        changed = project(environments=[{**PROJECT["environments"][0], "schema": "jade_dev2"}])
        await p.api.put("/projects/jade-ibf", json=changed)
        rerun = await p.api.post(f"/runs/{run['run_id']}/rerun", json={})
        assert rerun.status_code == 201, rerun.text
        await p.service.wait_idle()
        again = (await p.api.get(f"/runs/{rerun.json()['run_id']}")).json()
        assert again["project"]["params"] == {"update_month": "2026-10", "version": 3}
        assert "icsd-ace-dev.jade_dev2" in p.gateway.requests[1]["body"]["messages"][-1]["content"]
        old = (await p.api.get(f"/runs/{run['run_id']}")).json()
        assert old["project"]["environment"] == "dev"

        await p.api.delete("/projects/jade-ibf")
        refused = await p.api.post(f"/runs/{run['run_id']}/rerun", json={})
        assert refused.status_code == 422
        assert refused.json()["detail"] == "项目 jade-ibf 已经删除，不能重新运行"


@pytest.mark.parametrize(
    ("uses", "choice", "detail"),
    [
        (None, {"project_id": "jade-ibf"}, "应用「checks」不使用项目"),
        ("required", None, "应用「checks」需要选择一个项目"),
        ("required", {"project_id": "nope"}, "没有项目 nope"),
        (
            "required",
            {"project_id": "jade-ibf", "environment": "test"},
            "项目「IBF 入库运费」没有环境 test",
        ),
        (
            "required",
            {"project_id": "jade-ibf", "params": {"region": "x"}},
            "项目「IBF 入库运费」没有参数 region",
        ),
        (
            "required",
            {"project_id": "jade-ibf", "params": {"update_month": "9月"}},
            "参数「统计月」要写成 YYYY-MM",
        ),
    ],
)
async def test_binding_checks(uses, choice, detail):
    async with Platform([]) as p:
        await p.api.post("/projects", json=project())
        prompt = "{{start.question}}" if uses is None else None
        await p.new_app("checks", bound_flow(uses, prompt))
        resp = await start_run(p, "checks", choice)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == detail


async def test_required_parameters_and_parameters_the_app_uses():
    async with Platform(["好"]) as p:
        no_default = project(
            params=[{"key": "update_month", "label": "统计月", "type": "month", "required": True}]
        )
        await p.api.post("/projects", json=no_default)
        await p.new_app("bound", bound_flow())
        resp = await start_run(p, "bound", {"project_id": "jade-ibf"})
        assert resp.json()["detail"] == "请填写参数「统计月」"

        await p.new_app("other", bound_flow(prompt="{{param.region}} {{start.question}}"))
        given = {"project_id": "jade-ibf", "params": {"update_month": "2026-09"}}
        resp = await start_run(p, "other", given)
        assert resp.json()["detail"] == "应用用到了参数 region，项目「IBF 入库运费」没有定义它"

        await p.new_app("maybe", bound_flow("optional", prompt="[{{project.brief}}]"))
        resp = await start_run(p, "maybe", None)
        assert resp.status_code == 201, resp.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert run["project"] is None
        assert p.gateway.requests[-1]["body"]["messages"][-1]["content"] == "[]"


async def test_text_parameters_are_taken_without_spaces_around_them():
    """jadeflow retest S-19: a run_id of spaces only started a run."""
    store = MemoryProjectStore()
    batch = {"key": "run_id", "label": "运行批次", "type": "text", "required": True}
    saved = await store.create(ProjectBody.model_validate(project(params=[batch])).body())
    with pytest.raises(BindError, match="请填写参数「运行批次」"):
        snapshot(saved, None, {"run_id": "   "})
    assert snapshot(saved, None, {"run_id": " RUN_7 "})["values"] == {"run_id": "RUN_7"}


async def test_a_project_policy_applies_to_its_runs():
    async with Platform(["好"]) as p:
        strict = project(policy={"samples": "stats", "models": ["qwen"]})
        await p.api.post("/projects", json=strict)
        await p.new_app("bound", bound_flow())
        resp = await start_run(p, "bound", {"project_id": "jade-ibf"})
        assert resp.status_code == 422
        assert (
            resp.json()["detail"]
            == "项目「IBF 入库运费」只允许用模型 qwen，节点「回答」用的是 mock"
        )

        qwen = bound_flow()
        qwen["graph"]["nodes"][1]["model"] = "qwen"
        await p.new_app("bound-qwen", qwen)
        resp = await start_run(p, "bound-qwen", {"project_id": "jade-ibf"})
        assert resp.status_code == 201, resp.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert run["policy"] == {"samples": "stats", "models": ["qwen"]}


async def test_saving_checks_project_references():
    async with Platform([]) as p:
        resp = await p.api.post("/apps", json={"app": "refs", "title": "引用"})
        assert resp.status_code == 201
        unbound = bound_flow(None)
        saved = await p.api.put("/apps/refs/draft", json={"config": unbound})
        assert saved.status_code == 422
        assert "开始节点没有设置使用项目，不能引用项目和运行参数" in saved.text

        wrong_field = bound_flow(prompt="{{project.owner}}")
        saved = await p.api.put("/apps/refs/draft", json={"config": wrong_field})
        assert saved.status_code == 422
        assert "项目没有 owner 这一项" in saved.text

        odd = bound_flow("always")
        saved = await p.api.put("/apps/refs/draft", json={"config": odd})
        assert saved.status_code == 422
        assert "使用项目只能是 required（必须选）或 optional（可以不选）" in saved.text

        reserved = bound_flow()
        reserved["graph"]["nodes"][1]["id"] = "param"
        reserved["graph"]["edges"] = [
            {"from": "start", "to": "param"},
            {"from": "param", "to": "end"},
        ]
        saved = await p.api.put("/apps/refs/draft", json={"config": reserved})
        assert saved.status_code == 422
        assert "param 是保留的标识" in saved.text

        saved = await p.api.put("/apps/refs/draft", json={"config": bound_flow()})
        assert saved.status_code == 200, saved.text

        # A single-node test takes test values for {{param.*}} and {{project.*}}.
        tested = await p.api.post(
            "/apps/refs/nodes/ask/test",
            json={
                "config": bound_flow(),
                "values": {
                    "param.update_month": "2026-09",
                    "project.catalog": "c",
                    "project.schema": "s",
                    "project.brief": "",
                    "start.question": "多少？",
                },
            },
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["rendered"]["prompt"].startswith("统计月 2026-09，写到 c.s")


VERSION = {
    "key": "version_id",
    "label": "版本",
    "type": "text",
    "required": True,
    "pattern": r"FY[0-9]{2}_(0[1-9]|1[01])_M",
    "description": "月度版本 FY{yy}_{MM}_M，MM 是 01–11",
}
BATCH = {"key": "run_id", "label": "运行批次", "type": "text", "required": True}


ONLY_DATE = "只有按日期推算（date）才写 format、months、day"


def scheduled(param: dict[str, Any], **rule: Any) -> dict[str, Any]:
    return param | {"scheduled": rule}


@pytest.mark.parametrize(
    ("param", "detail"),
    [
        (
            scheduled(BATCH, use="run_id", months=1),
            "只有按日期推算（date）才写 format、months、day",
        ),
        (
            scheduled(BATCH, use="this_run", format="%Y"),
            "只有按日期推算（date）才写 format、months、day",
        ),
        (
            scheduled({**BATCH, "type": "number"}, use="run_id"),
            "作业运行的 ID 只能填进文本参数",
        ),
        (
            scheduled({**BATCH, "type": "boolean"}, use="date"),
            "按日期推算只能用在文本、日期或月份参数上",
        ),
        (scheduled(VERSION, use="date"), "文本参数要写日期格式 format，如 FY%y_%m_M"),
        (
            scheduled({**BATCH, "type": "month"}, use="date", format="%Y%m"),
            "月份参数的格式是固定的，不写 format",
        ),
        (scheduled({**BATCH, "type": "month"}, use="date", day="last"), "月份参数不用写 day"),
        (
            scheduled(VERSION, use="date", format="FY%y_%B"),
            "日期格式只能用 %Y（四位年份）、%y（两位年份）、%m（月）、%d（日）",
        ),
        (
            scheduled(VERSION, use="date", format="FY%%"),
            "日期格式里至少要有 %Y、%y、%m、%d 中的一个",
        ),
        (
            scheduled(BATCH, use="run_id", on_mismatch="skip"),
            "只有按日期推算（date）才写不符合格式时怎么办（on_mismatch、skip_note）",
        ),
        (
            scheduled({**BATCH, "type": "month"}, use="date", on_mismatch="skip"),
            "参数没有写格式，算出的日期都能用，不用写不符合格式时跳过",
        ),
        (
            scheduled(VERSION, use="date", format="FY%y_%m_M", skip_note="12 月不跑"),
            "skip_note 是跳过时写进运行记录的话，只有不符合格式时跳过（skip）才写",
        ),
    ],
)
def test_how_scheduled_runs_fill_in_a_parameter_is_checked(param, detail):
    with pytest.raises(ValueError, match="定时运行时的取法不对：") as caught:
        ProjectBody.model_validate(project(params=[param]))
    assert detail in str(caught.value)
    with pytest.raises(ValueError, match="greater than or equal to -24"):
        ProjectBody.model_validate(project(params=[scheduled(VERSION, use="date", months=-25)]))


async def test_scheduled_values_and_what_a_release_says():
    """jadeflow retest I-19: a released job's scheduled runs must not reuse the run's
    version and batch; the release step says how they are filled in."""
    from datetime import UTC, datetime

    from aep.projects import project_value, release_brief, scheduled_values

    store = MemoryProjectStore()
    month = {"key": "month", "label": "统计月", "type": "month", "default": "2026-10"}
    note = {"key": "note", "label": "备注", "type": "text"}
    params = [
        scheduled(VERSION, use="date", format="FY%y_%m_M", months=-1),
        scheduled(BATCH, use="run_id"),
        month,
        note,
        scheduled({**note, "key": "kind", "label": "口径"}, use="this_run"),
        scheduled({**month, "key": "close", "label": "关账日", "type": "date", "default": None},
                  use="date", months=-1, day="last"),
    ]  # fmt: skip
    body = ProjectBody.model_validate(project(params=params)).body()
    assert body["params"][0]["scheduled"] == {
        "use": "date",
        "format": "FY%y_%m_M",
        "months": -1,
        "day": None,
        "on_mismatch": "fail",
        "skip_note": None,
    }
    assert body["params"][2]["scheduled"] is None
    saved = await store.create(body)
    given = {"version_id": "FY26_06_M", "run_id": "RUN_JF_F", "kind": "按需求"}
    snap = snapshot(saved, "dev", given)

    # 2026-11-30 22:00 UTC is already 1 December in Shanghai.
    now = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)
    found = scheduled_values(snap, "Asia/Shanghai", now)
    assert found.today == "2026-12-01" and found.problems == []
    assert found.fixed == {
        "version_id": None,
        "run_id": None,
        "month": "2026-10",
        "note": None,
        "kind": "按需求",
        "close": None,
    }
    assert [(r["key"], r["use"]) for r in found.rules] == [
        ("version_id", "date"),
        ("run_id", "run_id"),
        ("close", "date"),
    ]
    assert found.rules[0] == {
        "key": "version_id",
        "label": "版本",
        "type": "text",
        "use": "date",
        "format": "FY%y_%m_M",
        "months": -1,
        "day": None,
        "pattern": VERSION["pattern"],
        "description": VERSION["description"],
        "on_mismatch": "fail",
        "skip_note": None,
    }
    assert [(line["how"], line["value"]) for line in found.lines] == [
        ("按运行那天推算上个月，写成 FY{yy}_{MM}_M", "FY26_11_M"),
        ("每次用 Databricks 作业运行的 ID", "（运行时才知道）"),
        ("用默认值", "2026-10"),
        ("不填（空值）", None),
        ("一直用这次运行的值", "按需求"),
        ("按运行那天推算上个月最后一天，写成 {yyyy}-{MM}-{dd}", "2026-11-30"),
    ]
    # In January the version would be FY26_12_M, which the parameter's format refuses.
    january = scheduled_values(snap, "Asia/Shanghai", datetime(2027, 1, 5, tzinfo=UTC))
    assert january.lines[0]["value"] == "FY26_12_M"
    assert january.lines[0]["warning"] == "这个值不符合参数的格式，这样的日子定时运行会报错、不写表"
    # 4th retest S-21: or such a day is skipped, which is no failure.
    skipping = {**params[0]["scheduled"], "on_mismatch": "skip", "skip_note": "12 月版本不跑"}
    later = snap | {"params": [{**snap["params"][0], "scheduled": skipping}]}
    january = scheduled_values(later, "Asia/Shanghai", datetime(2027, 1, 5, tzinfo=UTC))
    assert january.lines[0]["warning"] == (
        "这个值不符合参数的格式，这样的日子定时运行会跳过、不写表，不算失败：12 月版本不跑"
    )
    assert january.rules[0]["on_mismatch"] == "skip"

    assert project_value(snap, "can_release") is True
    assert project_value(snap, "release_ready", "Asia/Shanghai") is True
    said = release_brief(snap, "Asia/Shanghai", now)
    assert said.startswith("## 上线说明\n\n- 上线到项目「IBF 入库运费」的 生产 环境（prod）")
    assert "### 定时运行时的参数（按今天 2026-12-01，Asia/Shanghai）" in said
    assert "| 版本（`version_id`） | 按运行那天推算上个月，写成 FY{yy}_{MM}_M | FY26_11_M |" in said
    assert "### 怎么撤销" in said and "现在不能上线" not in said

    # A required parameter with neither a rule nor a default cannot be filled in.
    bare = snap | {"params": [VERSION, *snap["params"][1:]]}
    found = scheduled_values(bare, "Asia/Shanghai", now)
    assert found.problems == [
        "参数「版本」没有写定时运行时怎么取，也没有默认值：在项目的运行参数里给它写上定时运行时的"
        "取法（或者默认值），再重新发起运行"
    ]
    assert project_value(bare, "release_ready") is False
    assert "**现在不能上线：**" in release_brief(bare, "Asia/Shanghai", now)

    # No prod environment, a run in prod, an old snapshot, an unbound run.
    no_prod = snap | {"environments": snap["environments"][:1]}
    in_prod = snapshot(saved, "prod", given)
    old = {k: v for k, v in snap.items() if k != "environments"}
    for case in (no_prod, in_prod, old):
        assert project_value(case, "can_release") is False
        assert project_value(case, "release_ready") is False
        assert project_value(case, "release_brief") == ""
    assert project_value(None, "can_release") is None
    assert project_value(None, "release_brief") == ""
