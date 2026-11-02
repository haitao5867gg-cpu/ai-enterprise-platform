"""The 提交 PR node (docs/api/git.md): saving checks it, a run opens a pull request through
the Git connector, and code repositories are used nowhere else."""

import json
from typing import Any

import pytest

from tests import fake_git
from tests.test_flow_apps import end, flow
from tests.test_flow_tools import Platform

GEN = {
    "id": "gen",
    "type": "llm",
    "title": "写代码",
    "prompt": "{{start.question}}",
    "output": {
        "kind": "json",
        "fields": [{"key": "files", "type": "object", "description": "文件"}],
    },
}
FILES = {"src/job.py": "print('hi')\n", "README.md": "# job\n"}


def git_flow(source: str, **changes: Any) -> dict:
    step = {
        "id": "pr",
        "type": "git_pr",
        "title": "提交 PR",
        "datasource": source,
        "repo": fake_git.REPO,
        "directory": "pipelines/{{start.question}}",
        "files": "{{gen.files}}",
        "replace_directory": True,
        "pr_title": "新增 {{start.question}}",
        "message": "DATA-7 新增 {{start.question}}",
    } | changes
    return flow(
        GEN,
        step,
        end("{{pr.url}}"),
        edges=[
            {"from": "start", "to": "gen"},
            {"from": "gen", "to": "pr"},
            {"from": "pr", "to": "end"},
        ],
    )


class GitPlatform(Platform):
    def __init__(self, replies: list[str], git: fake_git.FakeGit) -> None:
        super().__init__(replies, servers={fake_git.URL: git.server})
        self.git = git

    async def __aenter__(self) -> "GitPlatform":
        await super().__aenter__()
        made = await self.sources.create("资产仓库", "git", fake_git.URL)
        self.repo = made.datasource_id
        return self


async def save(p: Platform, name: str, config: dict) -> Any:
    if (await p.api.get(f"/apps/{name}")).status_code == 404:
        made = await p.api.post("/apps", json={"app": name, "title": name})
        assert made.status_code == 201, made.text
    return await p.api.put(f"/apps/{name}/draft", json={"config": config})


def problems(resp: Any) -> dict[str, str]:
    assert resp.status_code == 422, resp.text
    return {".".join(str(x) for x in d["loc"][5:]): d["msg"] for d in resp.json()["detail"]}


@pytest.mark.parametrize(
    ("changes", "loc", "said"),
    [
        ({"datasource": ""}, "datasource", "要选择代码仓库"),
        ({"datasource": "nope"}, "datasource", "没有代码仓库 nope"),
        ({"repo": ""}, "repo", "要填仓库"),
        ({"files": ""}, "files", "要引用要提交的文件，比如 {{check_code.bundle.files}}"),
        ({"pr_title": ""}, "pr_title", "要填 PR 标题"),
        ({"pr_title": "长" * 201}, "pr_title", "PR 标题最长 200 个字符"),
        (
            {"branch": "aep/有 空格"},
            "branch",
            "分支名只能用字母、数字和 . _ / -，不写时平台按运行起名",
        ),
        ({"replace_directory": "yes"}, "replace_directory", "要选是或否"),
    ],
)
async def test_saving_checks_the_node(changes, loc, said):
    async with GitPlatform([], fake_git.FakeGit()) as p:
        found = problems(await save(p, "ship", git_flow(p.repo, **changes)))
        assert found.get(loc) == said, found


async def test_only_a_code_repository_and_only_in_this_node():
    async with GitPlatform([], fake_git.FakeGit()) as p:
        found = problems(await save(p, "ship", git_flow(p.shop)))
        assert found["datasource"] == "「示例库」不是代码仓库，这里要选代码仓库"
        tool = {
            "id": "t",
            "type": "tool",
            "title": "读仓库",
            "datasource": p.repo,
            "tool": "read_file",
            "args": {"repo": fake_git.REPO, "path": "README.md"},
        }
        config = flow(
            tool,
            end("{{t.result}}"),
            edges=[{"from": "start", "to": "t"}, {"from": "t", "to": "end"}],
        )
        found = problems(await save(p, "reader", config))
        assert found["datasource"] == "「资产仓库」是代码仓库，只能用在提交 PR 节点里"

        types = (await p.api.get("/node-types")).json()
        git_pr = next(t for t in types if t["type"] == "git_pr")
        assert git_pr["label"] == "提交 PR" and git_pr["category"] == "tool"
        assert git_pr["error_handling"]
        assert [o["key"] for o in git_pr["outputs"]] == [
            "number",
            "url",
            "branch",
            "commit",
            "created",
            "changed",
            "summary",
        ]


async def test_a_run_opens_a_pull_request_and_a_retry_reuses_it():
    git = fake_git.FakeGit()
    replies = [json.dumps({"files": FILES})] * 2
    async with GitPlatform(replies, git) as p:
        await p.new_app("ship", git_flow(p.repo))
        run = await p.run("ship", {"question": "orders"})
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"out": f"https://github.test/{fake_git.REPO}/pull/1"}

        ((tool, args),) = git.calls
        branch = f"aep/runs/{run['run_id'][:8]}/pr"
        assert tool == "open_pull_request"
        assert args["branch"] == branch and args["title"] == "新增 orders"
        assert args["message"] == "DATA-7 新增 orders"
        assert args["directory"] == "pipelines/orders" and args["replace_directory"] is True
        assert args["body"] == f"由平台运行生成：http://localhost:3000/runs/{run['run_id']}"
        assert args["base"] is None  # the default branch
        assert git.files(branch)["pipelines/orders/src/job.py"] == "print('hi')\n"
        assert git.commits[git.refs[branch]]["message"] == "DATA-7 新增 orders"
        said = [m["content"] for m in run["messages"] if m.get("node") == "pr"]
        assert said == [
            f"已在 {fake_git.REPO} 开 PR #1：https://github.test/{fake_git.REPO}/pull/1"
        ]
        step = next(
            s
            for s in (await p.api.get(f"/runs/{run['run_id']}/flow")).json()["steps"]
            if s["node"] == "pr"
        )
        outputs = step["output"]["vars"]["pr"]
        assert outputs["number"] == 1 and outputs["created"] is True
        assert outputs["changed"] == ["pipelines/orders/README.md", "pipelines/orders/src/job.py"]
        assert outputs["commit"] == git.refs[branch]

        # The same branch again (as a retried step does): the same PR, updated.
        await p.new_app("ship2", git_flow(p.repo, branch=branch, pr_title="改 {{start.question}}"))
        again = await p.run("ship2", {"question": "orders"})
        assert again["status"] == "done", again["error"]
        assert git.pulls[1]["title"] == "改 orders"
        said = [m["content"] for m in again["messages"] if m.get("node") == "pr"]
        assert said == [
            f"已更新 {fake_git.REPO} 的 PR #1：https://github.test/{fake_git.REPO}/pull/1"
        ]
        assert len(git.pulls) == 1


async def test_wrong_files_and_connector_errors_fail_the_node():
    git = fake_git.FakeGit()
    async with GitPlatform([json.dumps({"files": FILES})], git) as p:
        await p.new_app("bad", git_flow(p.repo, files="{{start.question}}"))
        run = await p.run("bad", {"question": "orders"})
        assert run["status"] == "error"
        assert run["failed"]["error_type"] == "input_error"
        assert run["error"].endswith("files 要引用一个 {路径: 文本} 对象")
        assert git.calls == []

        git.fail("open_pull_request", "GitHub 拒绝了访问（403），请检查令牌的权限：nope")
        await p.new_app("ship", git_flow(p.repo))
        run = await p.run("ship", {"question": "orders"})
        assert run["status"] == "error"
        assert run["failed"]["error_type"] == "tool_error" and run["failed"]["retryable"]
        assert run["error"].endswith("工具报错：GitHub 拒绝了访问（403），请检查令牌的权限：nope")
        assert git.pulls == {}


async def test_the_node_cannot_be_tried_alone():
    async with GitPlatform([], fake_git.FakeGit()) as p:
        await save(p, "ship", git_flow(p.repo))
        resp = await p.api.post("/apps/ship/nodes/pr/test", json={"values": {}})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "提交 PR 节点会真的在代码仓库开 PR，不能单独试跑"


async def test_the_repository_lists_the_apps_that_use_it():
    async with GitPlatform([], fake_git.FakeGit()) as p:
        await p.new_app("ship", git_flow(p.repo))
        used = (await p.api.get(f"/datasources/{p.repo}/usage")).json()["apps"]
        assert [(u["app"], u["via"]) for u in used] == [("ship", "literal")]
        exported = await p.api.get("/apps/ship/versions/2/export")
        assert "资产仓库" in exported.text
