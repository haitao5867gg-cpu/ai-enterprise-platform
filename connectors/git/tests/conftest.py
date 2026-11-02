import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from aep_git.config import Settings
from aep_git.hosts import GitError, PullRequest, RepoInfo


def git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    """Bare repositories acme/etl (main: README.md, pipelines/old/a.py, pipelines/old/b.py)
    and acme/other, as a Git host would serve them."""
    root = tmp_path / "remotes"
    for repo in ("acme/etl", "acme/other"):
        bare = root / f"{repo}.git"
        bare.mkdir(parents=True)
        git(bare, "init", "-q", "--bare", "-b", "main")
        seed = tmp_path / "seed" / repo
        seed.mkdir(parents=True)
        git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("# 资产仓库\n")
        (seed / "pipelines/old").mkdir(parents=True)
        (seed / "pipelines/old/a.py").write_text("a = 1\n")
        (seed / "pipelines/old/b.py").write_text("b = 1\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", str(bare), "main")
    return root


def settings(remotes: Path, tmp: Path, **changes: Any) -> Settings:
    values: dict[str, Any] = {
        "git_provider": "github",
        "git_token": SecretStr("token-for-tests"),
        "git_repos": ["acme/etl"],
        "git_clone_url": f"file://{remotes}/{{repo}}.git",
        "git_work_dir": str(tmp / "work"),
    }
    return Settings(_env_file=None, **(values | changes))  # type: ignore[call-arg]


class FakeHost:
    """Pull requests kept in memory, as a host would keep them."""

    def __init__(self) -> None:
        self.prs: dict[int, PullRequest] = {}
        self.bodies: dict[int, str] = {}
        self.comments: list[tuple[int, str]] = []

    async def repo(self, repo: str) -> RepoInfo:
        return RepoInfo(repo, "main", f"https://github.com/{repo}")

    async def find_open(self, repo: str, branch: str) -> PullRequest | None:
        return next(
            (p for p in self.prs.values() if p.branch == branch and p.state == "open"), None
        )

    async def create(self, repo: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        number = len(self.prs) + 1
        self.prs[number] = PullRequest(
            number, f"https://github.com/{repo}/pull/{number}", "open", title, branch, base
        )
        self.bodies[number] = body
        return self.prs[number]

    async def update(self, repo: str, number: int, title: str, body: str) -> PullRequest:
        old = self.prs[number]
        self.prs[number] = PullRequest(number, old.url, old.state, title, old.branch, old.base)
        self.bodies[number] = body
        return self.prs[number]

    async def get(self, repo: str, number: int) -> PullRequest:
        if number not in self.prs:
            raise GitError(f"GitHub 返回 404（GET /repos/{repo}/pulls/{number}）：Not Found")
        return self.prs[number]

    async def comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def close(self) -> None:
        return None
