"""A stand-in Git connector (connectors/git, docs/api/git.md): its six tools over one
repository kept in memory.

Trees are kept whole (path → content) under their sha; commits, branches, pull requests
and their comments live in plain dicts, as the host would keep them. ``merge``
fast-forwards the base branch to a PR's head, as a person merging it would. ``fail``
makes the next call of a tool answer with an error, worded as the connector words it.
"""

import hashlib
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aep.assets import AssetRepo
from aep.config import Settings
from aep.tools.mcp import McpTools

URL = "http://connector-git.test/mcp"
REPO = "acme/aep-assets"
PREFIX = "aep/"
TOOLS = (
    "list_repos",
    "list_files",
    "read_file",
    "open_pull_request",
    "get_pull_request",
    "comment_pull_request",
)


def _sha(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()


class FakeGit:
    def __init__(self, empty: bool = False, repos: tuple[str, ...] = (REPO,)) -> None:
        self.repos = repos
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.refs: dict[str, str] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.failures: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        if not empty:
            self.refs["main"] = self._commit({"README.md": "# assets\n"}, "init", [])
        self.server = self._build()

    def settings(self, **changes: Any) -> Settings:
        return Settings(**{"assets_repo": REPO, "assets_git_url": URL} | changes)

    def tools(self) -> McpTools:
        return McpTools(5, resolve=lambda url: self.server)

    def repo(self, **changes: Any) -> AssetRepo:
        return AssetRepo(self.settings(**changes), self.tools())

    def files(self, branch: str) -> dict[str, str]:
        return self.trees[self.commits[self.refs[branch]]["tree"]]

    def merge(self, number: int) -> None:
        pr = self.pulls[number]
        self.refs[pr["base"]] = self.refs[pr["head"]]
        pr["state"] = "merged"

    def fail(self, tool: str, message: str) -> None:
        self.failures[tool] = message

    def _commit(self, files: dict[str, str], message: str, parents: list[str]) -> str:
        tree = _sha(files)
        self.trees[tree] = files
        sha = _sha([tree, message, parents])
        self.commits[sha] = {"tree": tree, "message": message, "parents": parents}
        return sha

    def _check(self, tool: str, args: dict[str, Any]) -> None:
        self.calls.append((tool, args))
        if tool in self.failures:
            raise ToolError(self.failures.pop(tool))
        repo = args.get("repo")
        if repo is not None and repo not in self.repos:
            raise ToolError(f"没有允许访问仓库 {repo}")

    def _pr(self, repo: str, number: int) -> dict[str, Any]:
        if number not in self.pulls:
            raise ToolError(f"GitHub 返回 404（GET /repos/{repo}/pulls/{number}）：Not Found")
        pr = self.pulls[number]
        return {
            "repo": repo,
            "number": number,
            "url": pr["html_url"],
            "state": pr["state"],
            "title": pr["title"],
            "branch": pr["head"],
            "base": pr["base"],
        }

    def _build(self) -> MCPServer:
        server = MCPServer("fake-git")

        @server.tool()
        def list_repos() -> dict[str, Any]:
            self._check("list_repos", {})
            return {
                "provider": "github",
                "branch_prefix": PREFIX,
                "repos": [
                    {"repo": r, "default_branch": "main", "url": f"https://github.test/{r}"}
                    for r in self.repos
                ],
            }

        @server.tool()
        def list_files(repo: str, path: str = "", ref: str | None = None) -> dict[str, Any]:
            self._check("list_files", {"repo": repo, "path": path, "ref": ref})
            branch = ref or "main"
            files = self.files(branch)
            under = [p for p in sorted(files) if not path or p.startswith(path.rstrip("/") + "/")]
            return {
                "repo": repo,
                "ref": branch,
                "commit": self.refs[branch],
                "path": path,
                "files": [{"path": p, "type": "file", "size": len(files[p])} for p in under],
                "truncated": False,
            }

        @server.tool()
        def read_file(repo: str, path: str, ref: str | None = None) -> dict[str, Any]:
            self._check("read_file", {"repo": repo, "path": path, "ref": ref})
            branch = ref or "main"
            files = self.files(branch)
            if path not in files:
                raise ToolError(f"{branch} 上没有文件 {path}")
            return {
                "repo": repo,
                "ref": branch,
                "commit": self.refs[branch],
                "path": path,
                "content": files[path],
            }

        @server.tool()
        def open_pull_request(
            repo: str,
            branch: str,
            title: str,
            files: dict[str, str | None],
            message: str,
            body: str = "",
            base: str | None = None,
            directory: str = "",
            replace_directory: bool = False,
        ) -> dict[str, Any]:
            args = {
                "repo": repo,
                "branch": branch,
                "title": title,
                "files": files,
                "message": message,
                "body": body,
                "base": base,
                "directory": directory,
                "replace_directory": replace_directory,
            }
            self._check("open_pull_request", args)
            if not branch.startswith(PREFIX) or branch == PREFIX:
                raise ToolError(f"只能推送到 {PREFIX} 开头的分支，{branch} 不是")
            target = base or "main"
            if target not in self.refs:
                raise ToolError(f"仓库 {repo} 取不到 {target}：git fetch 失败")
            parent = self.refs[target]
            before = self.files(target)
            after = dict(before)
            under = directory.strip("/")
            wanted = {f"{under}/{p}" if under else p: c for p, c in files.items()}
            if replace_directory and under:
                for old in list(after):
                    if old.startswith(under + "/") and old not in wanted:
                        del after[old]
            for path, content in wanted.items():
                if content is None:
                    after.pop(path, None)
                else:
                    after[path] = content
            changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
            if not changed:
                raise ToolError(f"这些文件和 {target} 上的一样，没有可以提交的改动")
            sha = self._commit(after, message, [parent])
            self.refs[branch] = sha
            found = next(
                (p for p in self.pulls.values() if p["head"] == branch and p["state"] == "open"),
                None,
            )
            if found is None:
                number = len(self.pulls) + 1
                self.pulls[number] = {
                    "number": number,
                    "html_url": f"https://github.test/{repo}/pull/{number}",
                    "state": "open",
                    "title": title,
                    "head": branch,
                    "base": target,
                    "body": body,
                }
            else:
                number = found["number"]
                found.update(title=title, body=body)
            return {
                **self._pr(repo, number),
                "commit": sha,
                "changed": changed,
                "created": found is None,
            }

        @server.tool()
        def get_pull_request(repo: str, number: int) -> dict[str, Any]:
            self._check("get_pull_request", {"repo": repo, "number": number})
            return self._pr(repo, number)

        @server.tool()
        def comment_pull_request(repo: str, number: int, body: str) -> dict[str, Any]:
            self._check("comment_pull_request", {"repo": repo, "number": number, "body": body})
            self._pr(repo, number)
            self.comments.setdefault(number, []).append(body)
            return {"repo": repo, "number": number, "ok": True}

        return server
