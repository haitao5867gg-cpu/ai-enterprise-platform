"""The MCP tools as a client sees them, against bare repositories on disk and a fake host."""

from pathlib import Path

from mcp import Client
from pydantic import SecretStr

from aep_git.server import create_server
from tests.conftest import FakeHost, git, settings

TOOLS = {
    "list_repos",
    "list_files",
    "read_file",
    "open_pull_request",
    "get_pull_request",
    "comment_pull_request",
}


def branch_files(remotes: Path, tmp: Path, branch: str) -> dict[str, str]:
    """The files on ``branch`` of acme/etl, read from a fresh clone."""
    clone = tmp / "checks" / str(len(list(tmp.glob("checks/*"))))
    git(tmp, "clone", "-q", "-b", branch, str(remotes / "acme/etl.git"), str(clone))
    return {
        str(p.relative_to(clone)): p.read_text()
        for p in clone.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(clone).parts
    }


async def test_reads_files_of_allowed_repositories_only(remotes: Path, tmp_path: Path):
    host = FakeHost()
    async with Client(create_server(settings(remotes, tmp_path), host)) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == TOOLS
        writes = {t.name for t in tools if not (t.annotations and t.annotations.read_only_hint)}
        assert writes == {"open_pull_request", "comment_pull_request"}
        assert all(not t.annotations.destructive_hint for t in tools if t.annotations)

        repos = await client.call_tool("list_repos", {})
        assert repos.structured_content == {
            "provider": "github",
            "branch_prefix": "aep/",
            "repos": [
                {"repo": "acme/etl", "default_branch": "main", "url": "https://github.com/acme/etl"}
            ],
        }
        listed = await client.call_tool("list_files", {"repo": "acme/etl", "path": "pipelines"})
        assert not listed.is_error, listed.content
        assert listed.structured_content["ref"] == "main"
        assert listed.structured_content["files"] == [
            {"path": "pipelines/old", "type": "dir", "size": None},
            {"path": "pipelines/old/a.py", "type": "file", "size": 6},
            {"path": "pipelines/old/b.py", "type": "file", "size": 6},
        ]
        read = await client.call_tool("read_file", {"repo": "acme/etl", "path": "README.md"})
        assert read.structured_content["content"] == "# 资产仓库\n"
        assert len(read.structured_content["commit"]) == 40

        other = await client.call_tool("read_file", {"repo": "acme/other", "path": "README.md"})
        assert other.is_error and "没有允许访问仓库 acme/other" in other.content[0].text
        missing = await client.call_tool("read_file", {"repo": "acme/etl", "path": "nope.md"})
        assert missing.is_error and "main 上没有文件 nope.md" in missing.content[0].text
        for path in ["../x", "/etc/passwd", ".git/config", "a//b"]:
            bad = await client.call_tool("read_file", {"repo": "acme/etl", "path": path})
            assert bad.is_error, path
        nobranch = await client.call_tool(
            "list_files", {"repo": "acme/etl", "ref": "does-not-exist"}
        )
        assert nobranch.is_error and "仓库 acme/etl 取不到 does-not-exist" in (
            nobranch.content[0].text
        )


async def test_open_pull_request_commits_on_its_own_branch_and_reuses_the_pr(
    remotes: Path, tmp_path: Path
):
    host = FakeHost()
    async with Client(create_server(settings(remotes, tmp_path), host)) as client:
        args = {
            "repo": "acme/etl",
            "branch": "aep/etl/run-1",
            "title": "新增 retail_sales 管道",
            "files": {"databricks.yml": "bundle: x\n", "src/t.py": "print(1)\n"},
            "directory": "pipelines/retail_sales",
            "message": "DATA-42 新增 retail_sales 管道",
            "body": "由运行 run-1 生成",
        }
        first = await client.call_tool("open_pull_request", args)
        assert not first.is_error, first.content
        made = first.structured_content
        assert made["number"] == 1 and made["created"] is True
        assert made["branch"] == "aep/etl/run-1" and made["base"] == "main"
        assert made["changed"] == [
            "pipelines/retail_sales/databricks.yml",
            "pipelines/retail_sales/src/t.py",
        ]
        files = branch_files(remotes, tmp_path, "aep/etl/run-1")
        assert files["pipelines/retail_sales/src/t.py"] == "print(1)\n"
        assert files["README.md"] == "# 资产仓库\n"
        log = git(remotes / "acme/etl.git", "log", "--format=%an|%s", "aep/etl/run-1")
        assert log.splitlines() == ["AEP|DATA-42 新增 retail_sales 管道", "t|init"]
        # The token goes to git through its environment, never into the clone's config.
        config = (tmp_path / "work" / "acme_etl" / ".git" / "config").read_text()
        assert "token-for-tests" not in config
        # main is untouched.
        assert "pipelines/retail_sales/src/t.py" not in branch_files(remotes, tmp_path, "main")

        # Again with other files: the branch moves to a new commit on main, same PR.
        again = await client.call_tool(
            "open_pull_request",
            {**args, "files": {"databricks.yml": "bundle: y\n"}, "title": "改"},
        )
        assert not again.is_error, again.content
        assert again.structured_content["number"] == 1
        assert again.structured_content["created"] is False
        assert host.prs[1].title == "改"
        files = branch_files(remotes, tmp_path, "aep/etl/run-1")
        assert files["pipelines/retail_sales/databricks.yml"] == "bundle: y\n"
        assert "pipelines/retail_sales/src/t.py" not in files
        log = git(remotes / "acme/etl.git", "log", "--format=%s", "aep/etl/run-1")
        assert len(log.splitlines()) == 2


async def test_replacing_a_directory_and_deleting_files(remotes: Path, tmp_path: Path):
    async with Client(create_server(settings(remotes, tmp_path), FakeHost())) as client:
        result = await client.call_tool(
            "open_pull_request",
            {
                "repo": "acme/etl",
                "branch": "aep/replace",
                "title": "换掉 old",
                "files": {"a.py": "a = 2\n"},
                "directory": "pipelines/old",
                "replace_directory": True,
                "message": "换掉 old",
            },
        )
        assert not result.is_error, result.content
        assert result.structured_content["changed"] == ["pipelines/old/a.py", "pipelines/old/b.py"]
        files = branch_files(remotes, tmp_path, "aep/replace")
        assert files["pipelines/old/a.py"] == "a = 2\n"
        assert "pipelines/old/b.py" not in files

        deleted = await client.call_tool(
            "open_pull_request",
            {
                "repo": "acme/etl",
                "branch": "aep/delete",
                "title": "删 README",
                "files": {"README.md": None},
                "message": "删 README",
            },
        )
        assert deleted.structured_content["changed"] == ["README.md"]
        assert "README.md" not in branch_files(remotes, tmp_path, "aep/delete")


async def test_open_pull_request_refuses_what_it_must_not_do(remotes: Path, tmp_path: Path):
    host = FakeHost()
    async with Client(create_server(settings(remotes, tmp_path), host)) as client:
        base = {
            "repo": "acme/etl",
            "branch": "aep/x",
            "title": "t",
            "files": {"a.txt": "x"},
            "message": "m",
        }
        cases = [
            ({"branch": "main"}, "只能推送到 aep/ 开头的分支，main 不是"),
            ({"branch": "aep"}, "只能推送到 aep/ 开头的分支，aep 不是"),
            ({"branch": "aep/a..b"}, "分支名 aep/a..b 不合法"),
            ({"branch": "aep/a b"}, "分支名 aep/a b 不合法"),
            ({"files": {"../x": "1"}}, "不能有空段、. 或 .."),
            ({"files": {".git/hooks/pre-commit": "1"}}, "不能在 .git 里"),
            ({"directory": "/abs"}, "要写成仓库里的相对路径"),
            ({"files": {}}, "没有要提交的文件"),
            ({"files": {"README.md": "# 资产仓库\n"}}, "和 main 上的一样，没有可以提交的改动"),
            ({"repo": "acme/other"}, "没有允许访问仓库 acme/other"),
            ({"base": "nope"}, "仓库 acme/etl 取不到 nope"),
        ]
        for change, said in cases:
            result = await client.call_tool("open_pull_request", base | change)
            assert result.is_error, change
            assert said in result.content[0].text, (change, result.content[0].text)
        assert host.prs == {}
        heads = git(remotes / "acme/etl.git", "for-each-ref", "--format=%(refname)")
        assert heads.split() == ["refs/heads/main"]


async def test_pull_request_state_and_comments(remotes: Path, tmp_path: Path):
    host = FakeHost()
    async with Client(create_server(settings(remotes, tmp_path), host)) as client:
        await client.call_tool(
            "open_pull_request",
            {
                "repo": "acme/etl",
                "branch": "aep/c",
                "title": "t",
                "files": {"a.txt": "x"},
                "message": "m",
            },
        )
        got = await client.call_tool("get_pull_request", {"repo": "acme/etl", "number": 1})
        assert got.structured_content["state"] == "open"
        said = await client.call_tool(
            "comment_pull_request", {"repo": "acme/etl", "number": 1, "body": "已上线"}
        )
        assert not said.is_error
        assert host.comments == [(1, "已上线")]
        gone = await client.call_tool("get_pull_request", {"repo": "acme/etl", "number": 9})
        assert gone.is_error and "404" in gone.content[0].text


async def test_unconfigured_connector_says_what_is_missing(remotes: Path, tmp_path: Path):
    bare = settings(remotes, tmp_path, git_token=SecretStr(""), git_repos=[])
    async with Client(create_server(bare, FakeHost())) as client:
        result = await client.call_tool("list_repos", {})
        assert result.is_error
        assert "Git 连接器还没配置好，缺少：AEP_GIT_TOKEN、AEP_GIT_REPOS" in result.content[0].text


def test_clone_urls(remotes: Path, tmp_path: Path):
    plain = settings(remotes, tmp_path, git_clone_url="")
    assert plain.clone_url("acme/etl") == "https://github.com/acme/etl.git"
    ghe = settings(remotes, tmp_path, git_clone_url="", git_api_url="https://ghe.co/api/v3")
    assert ghe.clone_url("acme/etl") == "https://ghe.co/acme/etl.git"
    bb = settings(
        remotes,
        tmp_path,
        git_clone_url="",
        git_provider="bitbucket",
        git_api_url="https://bitbucket.co/",
    )
    assert bb.clone_url("DATA/Etl-Repo") == "https://bitbucket.co/scm/data/etl-repo.git"


def test_repositories_are_a_json_list_or_comma_separated(monkeypatch):
    from aep_git.config import Settings

    for value, repos in [
        ('["acme/etl", "DATA/etl"]', ["acme/etl", "DATA/etl"]),
        ("acme/etl, DATA/etl", ["acme/etl", "DATA/etl"]),
        ("", []),
    ]:
        monkeypatch.setenv("AEP_GIT_REPOS", value)
        assert Settings(_env_file=None).git_repos == repos  # type: ignore[call-arg]
