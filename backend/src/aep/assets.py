"""Company assets: generated bundles go to a Git repository as pull requests.

``AssetRepo`` writes a bundle under ``pipelines/<name>/`` on a new branch of the
assets repository and opens a PR, through the platform's Git connector (connectors/git,
docs/api/git.md): the platform holds no Git token and runs no git. Whether the PR merges
is up to people. ``AssetStore`` is the platform's index of every version: bundle name,
version, the PR and commit, the run it came from and where it stands.

Off unless AEP_ASSETS_REPO and AEP_ASSETS_GIT_URL are set.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg_pool import AsyncConnectionPool

from aep.config import Settings
from aep.runtime.store import DEFAULT_TENANT
from aep.tools.mcp import McpTools, ToolError, tool_message

# pending: the version is reserved, the PR not opened yet; proposed: the PR is open;
# released: approved and deployed to prod; rejected: the release was turned down (a
# later version replaces it); release_failed: approved, but the prod deploy failed.
AssetStatus = Literal["pending", "proposed", "released", "rejected", "release_failed"]


class AssetRepoError(Exception):
    pass


@dataclass(frozen=True)
class Published:
    number: int
    url: str
    branch: str
    commit: str


@dataclass(frozen=True)
class Asset:
    asset_id: str
    name: str
    version: int
    run_id: str
    revision: str
    status: AssetStatus
    pr_number: int | None = None
    pr_url: str = ""
    branch: str = ""
    commit_sha: str = ""
    tenant_id: str = DEFAULT_TENANT
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AssetRepo:
    """The assets repository, through the Git connector at ``assets_git_url`` (GitHub or
    Bitbucket Data Center, whichever that connector serves)."""

    def __init__(self, settings: Settings, tools: McpTools) -> None:
        self.repo = settings.assets_repo
        self.base = settings.assets_base_branch
        self._url = settings.assets_git_url
        self._tools = tools
        self._seconds = settings.assets_git_seconds

    @classmethod
    def from_settings(cls, settings: Settings, tools: McpTools) -> "AssetRepo | None":
        if not (settings.assets_repo and settings.assets_git_url):
            return None
        return cls(settings, tools)

    async def close(self) -> None:
        return None

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        try:
            return await self._tools.call(self._url, tool, args, self._seconds)
        except ToolError as exc:
            raise AssetRepoError(tool_message(exc, tool)) from exc

    async def open_pr(
        self,
        directory: str,
        files: dict[str, str],
        branch: str,
        title: str,
        body: str,
        message: str,
    ) -> Published:
        """Replace ``directory`` with ``files`` on ``branch`` (from the base branch) and
        open a PR to the base branch. Running it again for the same branch moves the branch
        to the new commit and reuses its open PR, so a retried step does not open a second."""
        made = await self._call(
            "open_pull_request",
            {
                "repo": self.repo,
                "branch": branch,
                "title": title,
                "files": files,
                "message": message,
                "body": body,
                "base": self.base,
                "directory": directory,
                "replace_directory": True,
            },
        )
        return Published(int(made["number"]), str(made["url"]), branch, str(made["commit"]))

    async def comment(self, number: int, body: str) -> None:
        await self._call(
            "comment_pull_request", {"repo": self.repo, "number": number, "body": body}
        )


class AssetStore(Protocol):
    """Versions are reserved before anything goes to GitHub: ``reserve`` hands out the next
    version of a bundle, and the same one again for the same run and code ``revision``, so
    a step that runs again (after a restart) reuses its version, branch and PR."""

    async def setup(self) -> None: ...
    async def reserve(self, name: str, run_id: str, revision: str) -> int: ...
    async def record(self, name: str, version: int, published: Published) -> Asset: ...
    async def set_status(self, name: str, version: int, status: AssetStatus) -> None: ...
    async def find(self, name: str | None = None) -> list[Asset]: ...


class MemoryAssetStore:
    """In-process index for tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Asset] = {}

    async def setup(self) -> None:
        return None

    async def reserve(self, name: str, run_id: str, revision: str) -> int:
        for asset in self._items.values():
            if (asset.name, asset.run_id, asset.revision) == (name, run_id, revision):
                return asset.version
        version = max((v for n, v in self._items if n == name), default=0) + 1
        self._items[(name, version)] = Asset(
            str(uuid.uuid4()), name, version, run_id, revision, "pending"
        )
        return version

    def _update(self, name: str, version: int, **changes: Any) -> Asset:
        asset = self._items[(name, version)]
        changes["updated_at"] = datetime.now(UTC)
        self._items[(name, version)] = Asset(**{**asset.__dict__, **changes})
        return self._items[(name, version)]

    async def record(self, name: str, version: int, published: Published) -> Asset:
        return self._update(
            name,
            version,
            status="proposed",
            pr_number=published.number,
            pr_url=published.url,
            branch=published.branch,
            commit_sha=published.commit,
        )

    async def set_status(self, name: str, version: int, status: AssetStatus) -> None:
        self._update(name, version, status=status)

    async def find(self, name: str | None = None) -> list[Asset]:
        found = [a for a in self._items.values() if name is None or a.name == name]
        return sorted(found, key=lambda a: (a.name, -a.version))


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS assets (
    asset_id   uuid PRIMARY KEY,
    tenant_id  text NOT NULL DEFAULT 'default',
    name       text NOT NULL,
    version    integer NOT NULL,
    run_id     text NOT NULL,
    revision   text NOT NULL,
    status     text NOT NULL,
    pr_number  integer,
    pr_url     text NOT NULL DEFAULT '',
    branch     text NOT NULL DEFAULT '',
    commit_sha text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name, version),
    UNIQUE (tenant_id, name, run_id, revision)
)""",
]

COLUMNS = (
    "asset_id::text, name, version, run_id, revision, status, pr_number, pr_url, branch,"
    " commit_sha, tenant_id, created_at, updated_at"
)


class PostgresAssetStore:
    """The asset index in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def reserve(self, name: str, run_id: str, revision: str) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            # One reservation of a bundle at a time, so two runs never read the same max().
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"assets:{DEFAULT_TENANT}:{name}",),
            )
            # Already reserved for this run and revision (the step running again): keep it.
            await conn.execute(
                "INSERT INTO assets (asset_id, tenant_id, name, version, run_id, revision, status)"
                " SELECT %s, %s, %s, coalesce(max(version), 0) + 1, %s, %s, 'pending'"
                " FROM assets WHERE tenant_id = %s AND name = %s"
                " ON CONFLICT (tenant_id, name, run_id, revision) DO NOTHING",
                (uuid.uuid4(), DEFAULT_TENANT, name, run_id, revision, DEFAULT_TENANT, name),
            )
            cur = await conn.execute(
                "SELECT version FROM assets"
                " WHERE tenant_id = %s AND name = %s AND run_id = %s AND revision = %s",
                (DEFAULT_TENANT, name, run_id, revision),
            )
            return (await cur.fetchone())["version"]

    async def record(self, name: str, version: int, published: Published) -> Asset:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE assets SET status = 'proposed', pr_number = %s, pr_url = %s, branch = %s,"
                " commit_sha = %s, updated_at = now()"
                f" WHERE tenant_id = %s AND name = %s AND version = %s RETURNING {COLUMNS}",
                (
                    published.number,
                    published.url,
                    published.branch,
                    published.commit,
                    DEFAULT_TENANT,
                    name,
                    version,
                ),
            )
            return Asset(**await cur.fetchone())

    async def set_status(self, name: str, version: int, status: AssetStatus) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE assets SET status = %s, updated_at = now()"
                " WHERE tenant_id = %s AND name = %s AND version = %s",
                (status, DEFAULT_TENANT, name, version),
            )

    async def find(self, name: str | None = None) -> list[Asset]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM assets WHERE tenant_id = %s"
                " AND (%s::text IS NULL OR name = %s) ORDER BY name, version DESC",
                (DEFAULT_TENANT, name, name),
            )
            return [Asset(**row) for row in await cur.fetchall()]
