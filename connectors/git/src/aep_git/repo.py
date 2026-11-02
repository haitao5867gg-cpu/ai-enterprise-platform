"""Reading and committing over git itself, the same for every host. Each repository has a
working copy under ``git_work_dir`` that is fetched (one commit deep) before each call; the
token goes to git through the environment, never on its command line or into a remote URL.
Commits are pushed only to branches under ``git_branch_prefix``."""

import asyncio
import base64
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from aep_git.config import Settings
from aep_git.hosts import GitError

MAX_FILES = 500
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 10_000_000
MAX_LISTED = 2000
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def check_path(path: str, what: str = "文件路径") -> str:
    """A path inside the repository, as git writes it: relative, with / between parts."""
    if not isinstance(path, str) or not path.strip():
        raise GitError(f"{what}不能为空")
    if "\\" in path or path.startswith("/") or "\x00" in path:
        raise GitError(f"{what} {path} 要写成仓库里的相对路径，用 / 分隔")
    parts = path.strip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise GitError(f"{what} {path} 不能有空段、. 或 ..")
    if parts[0] == ".git":
        raise GitError(f"{what} {path} 不能在 .git 里")
    return "/".join(parts)


def check_branch(branch: str, prefix: str) -> str:
    if (
        not _BRANCH.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith(("/", ".", ".lock"))
    ):
        raise GitError(f"分支名 {branch} 不合法：只能用字母、数字和 . _ / -")
    if not branch.startswith(prefix) or branch == prefix:
        raise GitError(f"只能推送到 {prefix} 开头的分支，{branch} 不是")
    return branch


@dataclass(frozen=True)
class Entry:
    path: str
    type: str  # file or dir
    size: int | None


@dataclass(frozen=True)
class Pushed:
    commit: str
    changed: list[str]


class Workspace:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = Path(settings.git_work_dir)
        self._locks: dict[str, asyncio.Lock] = {}

    def _env(self) -> dict[str, str]:
        token = self._settings.git_token.get_secret_value()
        if self._settings.git_provider == "github":
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            header = f"Authorization: Basic {basic}"
        else:
            header = f"Authorization: Bearer {token}"
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self._root),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": header,
            "GIT_AUTHOR_NAME": self._settings.git_author_name,
            "GIT_AUTHOR_EMAIL": self._settings.git_author_email,
            "GIT_COMMITTER_NAME": self._settings.git_author_name,
            "GIT_COMMITTER_EMAIL": self._settings.git_author_email,
        }

    async def _git(self, cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=self._env(),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin), self._settings.git_timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            seconds = self._settings.git_timeout_seconds
            raise GitError(f"git {args[0]} 超过 {seconds} 秒没有完成") from None
        if proc.returncode != 0:
            text = " ".join(err.decode(errors="replace").split())[:500]
            raise GitError(f"git {args[0]} 失败：{text}")
        return out

    def _dir(self, repo: str) -> Path:
        return self._root / re.sub(r"[^A-Za-z0-9._-]", "_", repo)

    async def _fetch(self, repo: str, ref: str) -> tuple[Path, str]:
        """Fetch ``ref`` (a branch or a full commit sha) one commit deep; its commit."""
        if not (_SHA.fullmatch(ref) or _BRANCH.fullmatch(ref)) or ".." in ref:
            raise GitError(f"分支或提交 {ref} 写法不对")
        path = self._dir(repo)
        if not (path / ".git").exists():
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
            await self._git(path, "init", "-q")
            await self._git(path, "remote", "add", "origin", self._settings.clone_url(repo))
        target = ref if _SHA.fullmatch(ref) else f"refs/heads/{ref}"
        try:
            await self._git(path, "fetch", "-q", "--depth", "1", "--no-tags", "origin", target)
        except GitError as exc:
            raise GitError(f"仓库 {repo} 取不到 {ref}：{exc}") from None
        sha = (await self._git(path, "rev-parse", "FETCH_HEAD")).decode().strip()
        return path, sha

    def lock(self, repo: str) -> asyncio.Lock:
        return self._locks.setdefault(repo, asyncio.Lock())

    async def list_files(self, repo: str, ref: str, path: str) -> tuple[str, list[Entry], bool]:
        under = check_path(path, "目录") if path else ""
        async with self.lock(repo):
            work, sha = await self._fetch(repo, ref)
            out = await self._git(
                work, "ls-tree", "-r", "-t", "-l", "-z", sha, *([under + "/"] if under else [])
            )
        entries: list[Entry] = []
        for line in out.split(b"\0"):
            if not line:
                continue
            meta, name = line.split(b"\t", 1)
            if under and not name.decode(errors="replace").startswith(under + "/"):
                continue
            _, kind, _, size = meta.split()
            entries.append(
                Entry(
                    name.decode(errors="replace"),
                    "dir" if kind == b"tree" else "file",
                    None if size == b"-" else int(size),
                )
            )
        return sha, entries[:MAX_LISTED], len(entries) > MAX_LISTED

    async def read_file(self, repo: str, ref: str, path: str) -> tuple[str, str]:
        name = check_path(path)
        async with self.lock(repo):
            work, sha = await self._fetch(repo, ref)
            try:
                size = int((await self._git(work, "cat-file", "-s", f"{sha}:{name}")).strip())
            except GitError:
                raise GitError(f"{ref} 上没有文件 {name}") from None
            if size > MAX_FILE_BYTES:
                raise GitError(f"文件 {name} 有 {size} 字节，超过 {MAX_FILE_BYTES} 字节，不读")
            data = await self._git(work, "cat-file", "blob", f"{sha}:{name}")
        try:
            return sha, data.decode()
        except UnicodeDecodeError:
            raise GitError(f"文件 {name} 不是 UTF-8 文本，不读") from None

    async def push(
        self,
        repo: str,
        base: str,
        branch: str,
        files: dict[str, str | None],
        directory: str,
        replace_directory: bool,
        message: str,
    ) -> Pushed:
        """One commit on top of ``base`` with ``files`` (None deletes), pushed to
        ``branch`` whatever it held: running it again gives the same change."""
        branch = check_branch(branch, self._settings.git_branch_prefix)
        under = check_path(directory, "目录") if directory else ""
        if not message.strip():
            raise GitError("提交信息不能为空")
        if not files:
            raise GitError("没有要提交的文件")
        if len(files) > MAX_FILES:
            raise GitError(f"一次最多提交 {MAX_FILES} 个文件，这次有 {len(files)} 个")
        wanted: dict[str, str | None] = {}
        total = 0
        for path, content in files.items():
            name = check_path(f"{under}/{path}" if under else path)
            if content is not None:
                size = len(content.encode())
                if size > MAX_FILE_BYTES:
                    raise GitError(f"文件 {name} 超过 {MAX_FILE_BYTES} 字节")
                total += size
            wanted[name] = content
        if total > MAX_TOTAL_BYTES:
            raise GitError(f"文件一共超过 {MAX_TOTAL_BYTES} 字节")
        async with self.lock(repo):
            work, parent = await self._fetch(repo, base)
            await self._git(work, "checkout", "-q", "--force", "--detach", parent)
            await self._git(work, "clean", "-q", "-f", "-d", "-x")
            if replace_directory and under:
                listed = await self._git(work, "ls-files", "-z", "--", under + "/")
                for old in listed.split(b"\0"):
                    if old and old.decode() not in wanted:
                        (work / old.decode()).unlink()
            for name, content in wanted.items():
                target = work / name
                if content is None:
                    if target.is_file():
                        target.unlink()
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="")
            await self._git(work, "add", "-A")
            changed = [
                line
                for line in (await self._git(work, "diff", "--cached", "--name-only", "-z"))
                .decode()
                .split("\0")
                if line
            ]
            if not changed:
                raise GitError(f"这些文件和 {base} 上的一样，没有可以提交的改动")
            await self._git(work, "commit", "-q", "--no-verify", "-F", "-", stdin=message.encode())
            sha = (await self._git(work, "rev-parse", "HEAD")).decode().strip()
            await self._git(work, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}")
        return Pushed(sha, changed)
