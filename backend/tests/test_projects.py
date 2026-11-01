"""Projects: the API, binding runs to them, {{param.*}} and {{project.*}}
(docs/api/projects.md)."""

import copy
from typing import Any

import pytest

from aep.config import get_settings
from aep.main import app
from aep.projects import filter_problem, param_problem
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
    assert param_problem({"label": "字", "type": "text"}, "") == "参数「字」要写 1–500 个字"
    choice = {"label": "版本", "type": "select", "choices": [{"value": "a"}, {"value": "b"}]}
    assert param_problem(choice, "c") == "参数「版本」要是这些之一：a、b"


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
        ({"policy": {"samples": "some"}}, "「数据外发策略的样本级别」的取值不对"),
    ],
)
async def test_project_rules(changes, detail):
    async with Platform([]) as p:
        resp = await p.api.post("/projects", json=project(**changes))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == detail


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
