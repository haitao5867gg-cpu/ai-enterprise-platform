"""The ``git_pr`` node (提交 PR): proposes files to a code repository as a pull request
through its Git connector (docs/api/git.md). Code leaves the platform only this way: the
connector pushes to its own aep/ branch and opens or updates the pull request; whether it
merges is up to people."""

import re
from typing import Any

from aep.datasources import GIT_KIND
from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    INPUT_ERROR,
    CheckEnv,
    NodeFailure,
    NodeType,
    Outcome,
    Problems,
    Runtime,
)

MAX_TITLE = 200
OUTPUTS = ("number", "url", "branch", "commit", "created", "changed", "summary")
# A branch written in the node, apart from its references.
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]*$")


def _required(node: dict[str, Any], key: str, p: Problems, said: str) -> None:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        p.add((key,), said)


async def check_git_pr(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    source = node.get("datasource")
    if not isinstance(source, str) or not source.strip():
        p.add(("datasource",), "要选择代码仓库")
    elif refs.whole(source) is None and env.strict and env.datasources is not None:
        found = await env.datasources.get(source)
        if found is None:
            p.add(("datasource",), f"没有代码仓库 {source}")
        elif found.kind != GIT_KIND:
            p.add(("datasource",), f"「{found.name}」不是代码仓库，这里要选代码仓库")
    _required(node, "repo", p, "要填仓库")
    _required(node, "files", p, "要引用要提交的文件，比如 {{check_code.bundle.files}}")
    _required(node, "pr_title", p, "要填 PR 标题")
    title = node.get("pr_title")
    if isinstance(title, str) and len(title) > MAX_TITLE:
        p.add(("pr_title",), f"PR 标题最长 {MAX_TITLE} 个字符")
    for key in ("base", "directory", "branch", "body", "message"):
        if not isinstance(node.get(key, ""), str):
            p.add((key,), "要写成文字")
    branch = node.get("branch", "")
    if isinstance(branch, str) and not _BRANCH.match(refs.REF.sub("x", branch)):
        p.add(("branch",), "分支名只能用字母、数字和 . _ / -，不写时平台按运行起名")
    if not isinstance(node.get("replace_directory", False), bool):
        p.add(("replace_directory",), "要选是或否")


def _files(value: Any) -> dict[str, str | None]:
    if (
        not isinstance(value, dict)
        or not value
        or not all(
            isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in value.items()
        )
    ):
        raise NodeFailure(INPUT_ERROR, "files 要引用一个 {路径: 文本} 对象")
    return value


async def run_git_pr(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)

    def text(key: str) -> str:
        return refs.as_text(refs.resolve(node.get(key) or "", get)).strip()

    source = await rt.source(refs.resolve(node["datasource"], get), git=True)
    if source.kind != GIT_KIND:
        raise NodeFailure(INPUT_ERROR, f"「{source.name}」不是代码仓库，这里要选代码仓库")
    run_id = str(state.get("run_id") or "")
    title = text("pr_title")[:MAX_TITLE]
    if not title:
        raise NodeFailure(INPUT_ERROR, "PR 标题是空的")
    body = text("body") or (
        f"由平台运行生成：{rt.settings.web_url.rstrip('/')}/runs/{run_id}" if run_id else ""
    )
    args: dict[str, Any] = {
        "repo": text("repo"),
        "branch": text("branch") or f"aep/runs/{run_id[:8] or 'test'}/{node['id']}",
        "title": title,
        "files": _files(refs.resolve(node["files"], get)),
        "message": text("message") or title,
        "body": body,
        "directory": text("directory"),
        "replace_directory": bool(node.get("replace_directory", False)),
    }
    if base := text("base"):
        args["base"] = base
    made = await rt.call(source, "open_pull_request", args)
    if not isinstance(made, dict) or not isinstance(made.get("number"), int):
        raise NodeFailure("tool_error", "代码仓库连接器没有返回 PR")
    repo = args["repo"]
    said = (
        f"已在 {repo} 开 PR #{made['number']}：{made.get('url', '')}"
        if made.get("created")
        else f"已更新 {repo} 的 PR #{made['number']}：{made.get('url', '')}"
    )
    return Outcome(
        {
            "number": made["number"],
            "url": made.get("url", ""),
            "branch": made.get("branch", args["branch"]),
            "commit": made.get("commit", ""),
            "created": bool(made.get("created")),
            "changed": made.get("changed", []),
            "summary": said,
        },
        messages=[{"role": "assistant", "content": said}],
    )


TYPES: dict[str, NodeType] = {
    "git_pr": NodeType(
        "git_pr",
        "提交 PR",
        "把文件以 PR 提交到代码仓库（GitHub 或 Bitbucket）：在平台自己的分支上提交，"
        "合不合并由人决定",
        "tool",
        "tool",
        check_git_pr,
        run_git_pr,
        lambda node: None,
        lambda node: list(OUTPUTS),
        outputs=(
            ("number", "PR 编号"),
            ("url", "PR 链接"),
            ("branch", "提交到的分支"),
            ("commit", "提交号"),
            ("created", "是否新开的 PR（否：更新了已有的）"),
            ("changed", "改动的文件"),
            ("summary", "一句话说明"),
        ),
        defaults={
            "datasource": "",
            "repo": "",
            "base": "",
            "directory": "",
            "files": "",
            "replace_directory": False,
            "branch": "",
            "pr_title": "",
            "body": "",
            "message": "",
        },
        error_handling=True,
    )
}
