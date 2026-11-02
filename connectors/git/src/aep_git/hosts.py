"""The pull request side of a Git host, over its REST API: GitHub (and GitHub Enterprise
Server) or Bitbucket Data Center. Branches and commits go over git itself (``aep_git.repo``);
only default branches and pull requests come from here."""

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx2 as httpx

from aep_git.config import Settings

ATTEMPTS = 3
RETRY_SECONDS = 1.0
MAX_ERROR = 500

PrState = Literal["open", "merged", "closed"]


class GitError(Exception):
    """A tool's failure, said so that a person can act on it."""


@dataclass(frozen=True)
class RepoInfo:
    repo: str
    default_branch: str
    url: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: PrState
    title: str
    branch: str
    base: str


class Host(Protocol):
    async def repo(self, repo: str) -> RepoInfo: ...
    async def find_open(self, repo: str, branch: str) -> PullRequest | None: ...
    async def create(
        self, repo: str, branch: str, base: str, title: str, body: str
    ) -> PullRequest: ...
    async def update(self, repo: str, number: int, title: str, body: str) -> PullRequest: ...
    async def get(self, repo: str, number: int) -> PullRequest: ...
    async def comment(self, repo: str, number: int, body: str) -> None: ...
    async def close(self) -> None: ...


def _short(text: object) -> str:
    text = " ".join(str(text or "").split())
    return text[:MAX_ERROR] + ("…" if len(text) > MAX_ERROR else "")


class _Rest:
    def __init__(
        self,
        name: str,
        base_url: str,
        headers: dict[str, str],
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self.name = name
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=httpx.Timeout(30.0), transport=transport
        )

    async def call(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = await self._client.request(method, path, json=body, params=params or None)
                break
            except httpx.TransportError:
                if attempt < ATTEMPTS:
                    await asyncio.sleep(RETRY_SECONDS * 2 ** (attempt - 1))
                    continue
                raise GitError(f"连不上 {self.name}，请检查地址和网络") from None
            except httpx.HTTPError:
                raise GitError(f"连不上 {self.name}，请检查地址和网络") from None
        if resp.status_code >= 400:
            try:
                data = resp.json()
                message = data.get("message") or "; ".join(
                    str(e.get("message", "")) for e in data.get("errors", [])
                )
            except (ValueError, AttributeError):
                message = resp.text
            if resp.status_code in (401, 403):
                raise GitError(
                    f"{self.name} 拒绝了访问（{resp.status_code}），请检查令牌的权限："
                    + _short(message)
                )
            raise GitError(
                f"{self.name} 返回 {resp.status_code}（{method} {path}）：{_short(message)}"
            )
        return resp.json() if resp.content else None

    async def close(self) -> None:
        await self._client.aclose()


class GitHub:
    """GitHub and GitHub Enterprise Server (REST v3)."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._rest = _Rest(
            "GitHub",
            settings.git_api_url.rstrip("/"),
            {
                "Authorization": f"Bearer {settings.git_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport,
        )

    @staticmethod
    def _pr(data: dict[str, Any]) -> PullRequest:
        state: PrState = (
            "merged" if data.get("merged_at") else "open" if data["state"] == "open" else "closed"
        )
        return PullRequest(
            int(data["number"]),
            str(data["html_url"]),
            state,
            str(data.get("title", "")),
            str(data["head"]["ref"]),
            str(data["base"]["ref"]),
        )

    async def repo(self, repo: str) -> RepoInfo:
        data = await self._rest.call("GET", f"/repos/{repo}")
        return RepoInfo(repo, str(data["default_branch"]), str(data["html_url"]))

    async def find_open(self, repo: str, branch: str) -> PullRequest | None:
        owner = repo.split("/")[0]
        found = await self._rest.call(
            "GET", f"/repos/{repo}/pulls", head=f"{owner}:{branch}", state="open"
        )
        return self._pr(found[0]) if found else None

    async def create(self, repo: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        data = await self._rest.call(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return self._pr(data)

    async def update(self, repo: str, number: int, title: str, body: str) -> PullRequest:
        data = await self._rest.call(
            "PATCH", f"/repos/{repo}/pulls/{number}", {"title": title, "body": body}
        )
        return self._pr(data)

    async def get(self, repo: str, number: int) -> PullRequest:
        return self._pr(await self._rest.call("GET", f"/repos/{repo}/pulls/{number}"))

    async def comment(self, repo: str, number: int, body: str) -> None:
        await self._rest.call("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    async def close(self) -> None:
        await self._rest.close()


class Bitbucket:
    """Bitbucket Data Center / Server (REST 1.0). Repositories are PROJECT/slug."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._rest = _Rest(
            "Bitbucket",
            settings.git_api_url.rstrip("/") + "/rest/api/1.0",
            {
                "Authorization": f"Bearer {settings.git_token.get_secret_value()}",
                "Accept": "application/json",
            },
            transport,
        )

    @staticmethod
    def _path(repo: str) -> str:
        project, slug = repo.split("/", 1)
        return f"/projects/{quote(project)}/repos/{quote(slug.lower())}"

    @staticmethod
    def _link(data: dict[str, Any]) -> str:
        links = data.get("links", {}).get("self") or [{}]
        return str(links[0].get("href", ""))

    def _pr(self, data: dict[str, Any]) -> PullRequest:
        state: PrState = {"OPEN": "open", "MERGED": "merged"}.get(str(data["state"]), "closed")
        return PullRequest(
            int(data["id"]),
            self._link(data),
            state,
            str(data.get("title", "")),
            str(data["fromRef"]["displayId"]),
            str(data["toRef"]["displayId"]),
        )

    async def repo(self, repo: str) -> RepoInfo:
        data = await self._rest.call("GET", self._path(repo))
        branch = await self._rest.call("GET", self._path(repo) + "/default-branch")
        return RepoInfo(repo, str(branch["displayId"]), self._link(data))

    async def find_open(self, repo: str, branch: str) -> PullRequest | None:
        found = await self._rest.call(
            "GET",
            self._path(repo) + "/pull-requests",
            state="OPEN",
            direction="OUTGOING",
            at=f"refs/heads/{branch}",
        )
        values = found.get("values", [])
        return self._pr(values[0]) if values else None

    async def create(self, repo: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        data = await self._rest.call(
            "POST",
            self._path(repo) + "/pull-requests",
            {
                "title": title,
                "description": body,
                "fromRef": {"id": f"refs/heads/{branch}"},
                "toRef": {"id": f"refs/heads/{base}"},
            },
        )
        return self._pr(data)

    async def update(self, repo: str, number: int, title: str, body: str) -> PullRequest:
        path = self._path(repo) + f"/pull-requests/{number}"
        current = await self._rest.call("GET", path)
        data = await self._rest.call(
            "PUT", path, {"version": current["version"], "title": title, "description": body}
        )
        return self._pr(data)

    async def get(self, repo: str, number: int) -> PullRequest:
        return self._pr(await self._rest.call("GET", self._path(repo) + f"/pull-requests/{number}"))

    async def comment(self, repo: str, number: int, body: str) -> None:
        await self._rest.call(
            "POST", self._path(repo) + f"/pull-requests/{number}/comments", {"text": body}
        )

    async def close(self) -> None:
        await self._rest.close()


def make_host(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> Host:
    if settings.git_provider == "bitbucket":
        return Bitbucket(settings, transport)
    return GitHub(settings, transport)
