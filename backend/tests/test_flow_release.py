"""The databricks_release node (docs/api/data-nodes.md, "第五批：上线到 prod"): what saving
checks, where it releases with and without a project, the schedule it takes, and why it
does not release."""

from typing import Any

import pytest

# The node types first: data_nodes is imported through them.
import aep.runtime.flow.nodes as flownodes
from aep.projects import Project, snapshot
from aep.runtime.flow.data_nodes import quartz_problem, schedule_text
from aep.skills import MemorySkillStore
from tests import fake_databricks
from tests.conftest import FakeGateway
from tests.test_databricks_job import job_bundle
from tests.test_project_writes import PROJECT, bound_bundle, dev_state, runtime

RELEASE = {"id": "release", "type": "databricks_release", "bundle": "{{check_code.bundle}}"}
PROD = {"databricks_prod_catalog": "icsd-ace-prod", "databricks_prod_schema": "sales_mart"}
ENVIRONMENTS = [
    PROJECT["environment"],
    {"key": "prod", "label": "生产", "catalog": "icsd-ace-prod", "schema": "jade"},
    {
        "key": "uat",
        "label": "验收",
        "catalog": "icsd-ace-uat",
        "schema": "jade",
        "cluster_id": "uat-cluster",
    },
]
BOUND = PROJECT | {"environments": ENVIRONMENTS}


async def check(**changes: Any) -> list[tuple[list, str]]:
    p = flownodes.Problems(())
    await flownodes.TYPES["databricks_release"].check(
        RELEASE | changes, p, flownodes.CheckEnv(None, MemorySkillStore())
    )
    return p.found


async def release(node: dict, state: dict, workspace, **settings: Any):
    return await flownodes.TYPES["databricks_release"].run(
        node, state, runtime(workspace, **settings)
    )


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("aep.databricks.POLL_SECONDS", 0)
    monkeypatch.setattr("aep.databricks.CLUSTER_POLL_SECONDS", 0)


async def test_saving_checks_the_bundle_schedule_and_environment():
    assert await check() == []
    assert await check(schedule="0 30 6 * * ?", environment="uat") == []
    assert await check(schedule="{{release_review.values}}") == []
    assert await check(schedule="0 0 {{start.hour}} * * ?") == []
    assert await check(bundle="") == [(["bundle"], "bundle不能为空")]
    quartz = "定时要写成 Quartz cron，如每天 2 点：0 0 2 * * ?"
    assert await check(schedule="0 2 * * *") == [(["schedule"], quartz)]
    assert await check(schedule="每天两点") == [(["schedule"], quartz)]
    assert await check(schedule=5) == [
        (["schedule"], "定时要写成文字，如 0 0 2 * * ?，或引用一个变量")
    ]
    assert await check(environment="Prod") == [
        (["environment"], "上线环境要写项目环境的标识，如 prod")
    ]


def test_quartz_and_how_a_schedule_is_said():
    assert quartz_problem("0 0 2 * * ?") is None
    assert quartz_problem("0 0 2 * * ? 2027") is None
    assert quartz_problem("0 15 10 ? * 6L") is None
    assert quartz_problem("0 0 2 * *") is not None
    assert quartz_problem("0 0 2 * * ? ; rm") is not None
    said = {
        "0 0 2 * * ?": "每天 02:00",
        "0 30 6 * * ? *": "每天 06:30",
        "0 0 * * * ?": "每小时整点",
        "0 15 * * * ?": "每小时第 15 分",
        "0 0 2 ? * MON": "每周一 02:00",
        "0 0 9 ? * 1": "每周日 09:00",
        "0 30 7 ? * MON-FRI": "周一到周五 07:30",
        "0 0 2 1 * ?": "每月 1 日 02:00",
        "0 15 10 ? * 6L": "按 Quartz cron 0 15 10 ? * 6L",
        "0 0 25 * * ?": "按 Quartz cron 0 0 25 * * ?",
        "30 0 2 * * ?": "按 Quartz cron 30 0 2 * * ?",
    }
    for cron, text in said.items():
        assert schedule_text(cron, "Asia/Shanghai") == f"{text}（Asia/Shanghai）"


def unbound(**vars: Any) -> dict:
    state = dev_state(job_bundle(), project=None)
    state["vars"] |= vars
    return state


async def test_releases_to_the_platforms_prod_target_on_the_schedule_given(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    settings = {"databricks_cluster_id": fake_databricks.CLUSTER_ID, **PROD}
    node = RELEASE | {"schedule": "{{release_review.values}}"}
    # The approval's values: the question keyed "schedule" (spaces as typed).
    state = unbound(release_review={"values": {"schedule": "0  0 2 ? * MON"}})

    done = await release(node, state, workspace, **settings)

    assert done.branch == "deployed"
    out = done.outputs
    assert (out["status"], out["ok"], out["job_id"]) == ("deployed", True, 100)
    assert out["url"] == f"{fake_databricks.HOST}/#job/100"
    assert (out["schedule"], out["schedule_text"]) == (
        "0 0 2 ? * MON",
        "每周一 02:00（Asia/Shanghai）",
    )
    assert out["summary"] == (
        "已上线到 prod：Job 100，每周一 02:00（Asia/Shanghai）刷新 `icsd-ace-prod`.sales_mart"
    )
    assert out["release"]["environment"] == "prod"
    assert out["release"]["job_name"] == "shop_orders"
    assert (out["release"]["catalog"], out["release"]["schema"]) == ("icsd-ace-prod", "sales_mart")
    assert "## 已上线" in out["report"] and "- 定时：每周一 02:00（Asia/Shanghai）" in out["report"]
    assert out["errors"] == ""
    assert done.messages == [{"role": "assistant", "content": out["summary"]}]
    job = workspace.jobs[100]
    assert job["schedule"]["quartz_cron_expression"] == "0 0 2 ? * MON"
    assert workspace.job_runs == []

    # No schedule given (or an empty answer): the platform's; a second release updates the job.
    for given in ("", None):
        state["vars"]["release_review"] = {"values": {"schedule": given}}
        again = await release(node, state, workspace, **settings)
        assert (again.branch, again.outputs["job_id"]) == ("deployed", 100)
        assert again.outputs["schedule"] == "0 0 2 * * ?"
    plain = await release(RELEASE | {"schedule": "0 30 6 * * ?"}, state, workspace, **settings)
    assert plain.outputs["schedule"] == "0 30 6 * * ?"
    assert list(workspace.jobs) == [100]


async def test_a_bad_schedule_or_bundle_is_an_input_error(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    settings = {"databricks_cluster_id": fake_databricks.CLUSTER_ID, **PROD}
    for value in ("每天两点", 5, {"schedule": "0 2 * * *"}):
        state = unbound(release_review={"values": value})
        with pytest.raises(flownodes.NodeFailure) as failed:
            await release(
                RELEASE | {"schedule": "{{release_review.values}}"}, state, workspace, **settings
            )
        assert (failed.value.type, failed.value.message) == (
            "input_error",
            "定时要写成 Quartz cron，如每天 2 点：0 0 2 * * ?",
        )
    state = unbound()
    state["vars"]["check_code"]["bundle"] = None
    with pytest.raises(flownodes.NodeFailure) as failed:
        await release(RELEASE, state, workspace, **settings)
    assert failed.value.type == "input_error"
    assert workspace.jobs == {}


async def test_what_cannot_be_released_takes_the_failed_branch(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    no_workspace = flownodes.Runtime(FakeGateway().client, "mock", {}, {})
    with pytest.raises(flownodes.NodeFailure) as failed:
        await flownodes.TYPES["databricks_release"].run(RELEASE, unbound(), no_workspace)
    assert failed.value.type == "databricks_error"

    # No prod target; the cluster way; a workspace that refuses.
    for settings, reason in (
        ({"databricks_cluster_id": "c"}, "AEP_DATABRICKS_PROD_CATALOG"),
        ({"databricks_cluster_id": "c", "databricks_deploy": "cluster", **PROD}, "cluster 模式"),
        ({"databricks_cluster_id": "c", "databricks_token": "wrong", **PROD}, "HTTP 401"),
    ):
        done = await release(RELEASE, unbound(), workspace, **settings)
        assert (done.branch, done.outputs["ok"], done.outputs["url"]) == ("failed", False, None)
        assert reason in done.outputs["errors"]
        assert done.outputs["summary"].startswith("上线到 prod 失败：")
        assert "## 上线失败" in done.outputs["report"] and "### 原因" in done.outputs["report"]
    assert workspace.jobs == {}


async def test_a_bound_run_releases_to_its_projects_environment(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    # The platform's own prod target is not where a project's run goes.
    settings = {"databricks_cluster_id": "9999-other", "databricks_deploy": "job", **PROD}
    state = dev_state(bound_bundle(), BOUND)

    done = await release(RELEASE | {"environment": "uat"}, state, workspace, **settings)

    assert done.branch == "deployed", done.outputs["errors"]
    out = done.outputs
    assert out["release"]["environment"] == "uat"
    assert out["release"]["job_name"] == "ibf_table2（jade-ibf/uat）"
    assert (out["release"]["catalog"], out["release"]["schema"]) == ("icsd-ace-uat", "jade")
    assert out["release"]["cluster_id"] == "uat-cluster"
    assert out["summary"].startswith("已上线到 uat：Job 100，每天 02:00（Asia/Shanghai）刷新")
    job = workspace.jobs[100]
    assert job["name"] == "ibf_table2（jade-ibf/uat）"
    assert job["tags"] == {
        "aep_bundle": "ibf_table2",
        "aep_target": "prod",
        "aep_project": "jade-ibf",
        "aep_environment": "uat",
    }
    task = job["tasks"][0]
    assert task["existing_cluster_id"] == "uat-cluster"
    parameters = task["notebook_task"]["base_parameters"]
    assert (parameters["catalog"], parameters["schema"]) == ("icsd-ace-uat", "jade")
    assert (parameters["table_prefix"], parameters["run_id"]) == ("ibf_", "{{job.run_id}}")
    assert "/.bundle/ibf_table2/uat/releases/" in task["notebook_task"]["notebook_path"]

    # prod is the default; it names no cluster, so the platform's prod or dev cluster runs it.
    prod = await release(
        RELEASE, state, workspace, **settings, databricks_prod_cluster_id="prod-cluster"
    )
    assert prod.branch == "deployed"
    assert prod.outputs["release"]["job_name"] == "ibf_table2（jade-ibf/prod）"
    assert workspace.jobs[101]["tasks"][0]["existing_cluster_id"] == "prod-cluster"
    assert sorted(workspace.jobs) == [100, 101]


@pytest.mark.parametrize(
    ("project", "settings", "reason"),
    [
        (BOUND, {"databricks_deploy": "pipeline"}, "绑定项目的运行请用 job 方式上线"),
        (BOUND | {"environments": ENVIRONMENTS[:1]}, {}, "项目「IBF 预测」没有环境 prod"),
        (PROJECT, {}, "这次运行绑定项目时还没有记下项目的全部环境，请重新发起运行"),
        (BOUND | {"table_prefix": "jade_"}, {}, "不以项目「IBF 预测」的表名前缀 jade_ 开头"),
    ],
)
async def test_a_bound_run_that_cannot_release_says_why(tmp_path, project, settings, reason):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    settings = {"databricks_cluster_id": "c", "databricks_deploy": "job"} | settings
    done = await release(RELEASE, dev_state(bound_bundle(), project), workspace, **settings)
    assert done.branch == "failed"
    assert reason in done.outputs["errors"]
    assert workspace.jobs == {} and workspace.requests == []


async def test_the_environment_tried_on_is_not_released_to(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    state = dev_state(bound_bundle(), BOUND)
    node = RELEASE | {"environment": "dev"}
    done = await release(node, state, workspace, databricks_cluster_id="c")
    assert done.outputs["errors"] == "上线的环境 dev 就是这次试算的环境，请选别的环境"


def test_a_runs_snapshot_keeps_every_environment():
    body = {
        "project_id": "jade-ibf",
        "name": "IBF 预测",
        "description": "",
        "table_prefix": "ibf_",
        "environments": ENVIRONMENTS,
        "references": [],
        "adopted": [],
        "params": [],
        "checks": [],
        "policy": {"samples": None, "models": None},
    }
    project = Project("jade-ibf", "IBF 预测", body)
    taken = snapshot(project, "uat", None)
    assert taken["environment"] == ENVIRONMENTS[2]
    assert taken["environments"] == ENVIRONMENTS


def rule(**found: Any) -> dict[str, Any]:
    """A parameter's ``scheduled`` as a project stores it."""
    return {"use": "date", "format": None, "months": 0, "day": None} | found


async def test_scheduled_runs_of_a_released_job_work_out_their_parameters(tmp_path):
    """jadeflow retest I-19: the job no longer reruns the released run's version."""
    import json

    workspace = fake_databricks.FakeWorkspace(tmp_path)
    settings = {"databricks_cluster_id": "c", "databricks_deploy": "job"}
    month, cutoff, scale = PROJECT["params"]
    batch = {"key": "batch", "label": "运行批次", "type": "text", "required": True}
    project = BOUND | {
        "params": [
            month | {"scheduled": rule(months=-1)},
            cutoff | {"default": "2026-01-01"},
            scale | {"scheduled": rule(use="this_run", months=0)},
            batch | {"pattern": "[0-9]+", "scheduled": rule(use="run_id")},
        ],
        "values": PROJECT["values"] | {"batch": "RUN_7"},
    }
    done = await release(RELEASE, dev_state(bound_bundle(), project), workspace, **settings)

    assert done.branch == "deployed", done.outputs["errors"]
    parameters = workspace.jobs[100]["tasks"][0]["notebook_task"]["base_parameters"]
    # Fixed at the release: the default, this run's value, and none for the worked-out ones.
    assert json.loads(parameters["params"]) == {
        "update_month": None,
        "cutoff": "2026-01-01",
        "scale": 1.5,
        "batch": None,
    }
    rules = json.loads(parameters["param_rules"])
    assert rules["timezone"] == "Asia/Shanghai"
    assert [(r["key"], r["type"], r["use"], r["months"]) for r in rules["params"]] == [
        ("update_month", "month", "date", -1),
        ("batch", "text", "run_id", 0),
    ]
    assert rules["params"][1]["pattern"] == "[0-9]+"
    report = done.outputs["report"]
    assert "- 下一次运行：" in report
    assert "### 定时运行时的参数" in report
    assert "| 统计月（`update_month`） | 按运行那天推算上个月，写成 {yyyy}-{MM} |" in report
    assert "| 运行批次（`batch`） | 每次用 Databricks 作业运行的 ID | （运行时才知道） |" in report
    assert "| 系数（`scale`） | 一直用这次运行的值 | 1.5 |" in report
    assert "删掉「ibf_table2（jade-ibf/prod）」" in report

    # A required parameter the scheduled runs cannot fill in: nothing is deployed.
    stuck = project | {"params": [*project["params"][:3], batch]}
    done = await release(RELEASE, dev_state(bound_bundle(), stuck), workspace, **settings)
    assert done.branch == "failed"
    assert done.outputs["errors"].startswith("参数「运行批次」没有写定时运行时怎么取，也没有默认值")
    # Code generated before the platform knew how: it cannot pass the rules.
    old = bound_bundle()
    old.files["databricks.yml"] = old.files["databricks.yml"].replace("param_rules", "rules_x")
    done = await release(RELEASE, dev_state(old, project), workspace, **settings)
    assert done.outputs["errors"] == (
        "这次运行的代码是平台升级前生成的，定时运行时没法按项目的规则算参数，请重新发起运行"
    )
    assert sorted(workspace.jobs) == [100]
