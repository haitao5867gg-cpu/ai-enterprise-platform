"""Official MCP SDK server, served over Streamable HTTP at /mcp."""

from collections.abc import Awaitable
from typing import Annotated, Any, TypeVar

import httpx2 as httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from aep_git.config import Settings
from aep_git.hosts import GitError, Host, PullRequest, make_host
from aep_git.repo import Workspace

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
# Pushes only to the connector's own branches and opens or edits pull requests: nothing
# on any other branch changes, and the same call again gives the same result.
PROPOSE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

Repo = Annotated[str, Field(description="owner/name on GitHub, PROJECT/slug on Bitbucket")]
Ref = Annotated[
    str | None, Field(description="Branch or full commit sha; the default branch when empty")
]
T = TypeVar("T")


def http_security(settings: Settings) -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.git_mcp_allowed_hosts,
        allowed_origins=settings.git_mcp_allowed_origins,
    )


def pr_dict(repo: str, pr: PullRequest) -> dict[str, Any]:
    return {
        "repo": repo,
        "number": pr.number,
        "url": pr.url,
        "state": pr.state,
        "title": pr.title,
        "branch": pr.branch,
        "base": pr.base,
    }


def create_server(
    settings: Settings | None = None,
    host: Host | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MCPServer:
    settings = settings or Settings()
    api = host or make_host(settings, transport)
    work = Workspace(settings)
    server = MCPServer(
        "aep-git",
        version="0.1.0",
        instructions=(
            "A code repository on GitHub or Bitbucket Data Center. Reads files, and proposes "
            "changes only as pull requests from branches starting with "
            f"{settings.git_branch_prefix}; it never pushes to any other branch."
        ),
    )

    def allowed(repo: str) -> str:
        missing = settings.missing()
        if missing:
            raise GitError("Git 连接器还没配置好，缺少：" + "、".join(missing))
        if repo not in settings.git_repos:
            raise GitError(f"没有允许访问仓库 {repo}")
        return repo

    async def call(operation: Awaitable[T]) -> T:
        try:
            return await operation
        except GitError as exc:
            raise ToolError(str(exc)) from None

    async def default_branch(repo: str, ref: str | None) -> str:
        return ref or (await api.repo(repo)).default_branch

    @server.tool(annotations=READ_ONLY)
    async def list_repos() -> dict[str, Any]:
        """The repositories this connector may use, with their default branch and web page."""

        async def run() -> dict[str, Any]:
            if settings.missing():
                allowed("")
            repos = [await api.repo(repo) for repo in settings.git_repos]
            return {
                "provider": settings.git_provider,
                "branch_prefix": settings.git_branch_prefix,
                "repos": [
                    {"repo": r.repo, "default_branch": r.default_branch, "url": r.url}
                    for r in repos
                ],
            }

        return await call(run())

    @server.tool(annotations=READ_ONLY)
    async def list_files(repo: Repo, path: str = "", ref: Ref = None) -> dict[str, Any]:
        """Files and directories under ``path`` (the whole repository when empty), at most
        2000, with sizes in bytes."""

        async def run() -> dict[str, Any]:
            branch = await default_branch(allowed(repo), ref)
            sha, entries, truncated = await work.list_files(repo, branch, path)
            return {
                "repo": repo,
                "ref": branch,
                "commit": sha,
                "path": path,
                "files": [{"path": e.path, "type": e.type, "size": e.size} for e in entries],
                "truncated": truncated,
            }

        return await call(run())

    @server.tool(annotations=READ_ONLY)
    async def read_file(repo: Repo, path: str, ref: Ref = None) -> dict[str, Any]:
        """One UTF-8 text file of at most 1 MB."""

        async def run() -> dict[str, Any]:
            branch = await default_branch(allowed(repo), ref)
            sha, content = await work.read_file(repo, branch, path)
            return {"repo": repo, "ref": branch, "commit": sha, "path": path, "content": content}

        return await call(run())

    @server.tool(annotations=PROPOSE)
    async def open_pull_request(
        repo: Repo,
        branch: Annotated[
            str, Field(description=f"Starts with {settings.git_branch_prefix}; made or replaced")
        ],
        title: Annotated[str, Field(min_length=1, max_length=200)],
        files: Annotated[
            dict[str, str | None],
            Field(description="{path: text}; null deletes the file. Paths are under directory"),
        ],
        message: Annotated[
            str,
            Field(min_length=1, max_length=5000, description="Commit message, e.g. with a ticket"),
        ],
        body: Annotated[str, Field(max_length=60000)] = "",
        base: Ref = None,
        directory: Annotated[
            str, Field(description="Where the files go; the repository root when empty")
        ] = "",
        replace_directory: Annotated[
            bool, Field(description="Delete the files under directory that files does not have")
        ] = False,
    ) -> dict[str, Any]:
        """Commit ``files`` on top of ``base`` (the default branch when empty) as one commit,
        put ``branch`` on it and open a pull request into ``base``. Called again with the
        same branch it replaces the commit and keeps the open pull request (its title and
        description are updated), so a retried step does not open a second one. The pull
        request is an ordinary diff; whether it merges is up to people."""

        async def run() -> dict[str, Any]:
            target = await default_branch(allowed(repo), base)
            pushed = await work.push(
                repo, target, branch, files, directory, replace_directory, message
            )
            found = await api.find_open(repo, branch)
            if found is None:
                pr = await api.create(repo, branch, target, title, body)
            else:
                pr = await api.update(repo, found.number, title, body)
            return {
                **pr_dict(repo, pr),
                "commit": pushed.commit,
                "changed": pushed.changed,
                "created": found is None,
            }

        return await call(run())

    @server.tool(annotations=READ_ONLY)
    async def get_pull_request(repo: Repo, number: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """Where a pull request stands: open, merged or closed (declined)."""

        async def run() -> dict[str, Any]:
            return pr_dict(repo, await api.get(allowed(repo), number))

        return await call(run())

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    async def comment_pull_request(
        repo: Repo,
        number: Annotated[int, Field(ge=1)],
        body: Annotated[str, Field(min_length=1, max_length=60000)],
    ) -> dict[str, Any]:
        """Add a comment to a pull request."""

        async def run() -> dict[str, Any]:
            await api.comment(allowed(repo), number, body)
            return {"repo": repo, "number": number, "ok": True}

        return await call(run())

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        # Without ?probe=1 nothing reaches the Git host.
        missing = settings.missing()
        if request.query_params.get("probe") != "1":
            return JSONResponse({"status": "ok", "configured": not missing, "missing": missing})
        if missing:
            return JSONResponse(
                {"status": "error", "problem": "缺少设置：" + "、".join(missing)}, status_code=503
            )
        try:
            for repo in settings.git_repos:
                await api.repo(repo)
        except GitError as exc:
            return JSONResponse({"status": "error", "problem": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok", "configured": True, "missing": []})

    return server


def main() -> None:
    settings = Settings()
    create_server(settings).run(
        transport="streamable-http",
        host=settings.git_mcp_host,
        port=settings.git_mcp_port,
        stateless_http=True,
        json_response=True,
        transport_security=http_security(settings),
    )


if __name__ == "__main__":
    main()
