"""Media paths: relative to the media volume, either a library file or a work file.

Library files (``<library_id>/<item_id>/<name>``) are only read. Everything the
connector makes goes under ``_work/<task>/``; the platform moves what it keeps into a
library (docs/api/media-processing.md).
"""

import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Any

WORK = "_work"
JOBS = ".jobs"
CACHE = ".cache"

VIDEO = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp"}


class MediaError(ValueError):
    """A problem to report to the caller as a tool error, in its words."""


def kind_of(name: str) -> str | None:
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in VIDEO:
        return "video"
    if suffix in AUDIO:
        return "audio"
    if suffix in IMAGE:
        return "image"
    return None


def source_path(value: Any) -> str:
    """The media path a ``source`` argument names: a path, a summary or a file reference."""
    if isinstance(value, dict):
        if isinstance(value.get("aep_file"), dict):
            value = value["aep_file"].get("path")
        else:
            value = value.get("path")
    if not isinstance(value, str) or not value.strip():
        raise MediaError("素材要写媒体路径，或带 path 的素材摘要、文件引用")
    return value.strip()


class Media:
    """Resolves media paths inside the volume and hands out work directories."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def work(self) -> Path:
        return self.root / WORK

    def prepare(self) -> None:
        for path in (self.work, self.work / JOBS, self.work / CACHE):
            path.mkdir(parents=True, exist_ok=True)

    def resolve(self, value: Any) -> Path:
        """The file a source names; refuses anything that could leave the volume."""
        text = source_path(value)
        relative = PurePosixPath(text)
        if (
            text.startswith(("/", "\\"))
            or "\\" in text
            or "\x00" in text
            or relative.is_absolute()
            or any(part in ("..", ".", "") for part in text.split("/"))
        ):
            raise MediaError(f"路径不对：{text}")
        path = self.root
        for part in relative.parts:
            path = path / part
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                raise MediaError(f"文件不存在：{text}") from None
            if stat.S_ISLNK(mode):
                raise MediaError(f"路径不对：{text}")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise MediaError(f"文件不存在：{text}")
        real = Path(os.path.realpath(path))
        if self.root not in real.parents:
            raise MediaError(f"路径不对：{text}")
        return real

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def task_dir(self, task: str | None = None) -> Path:
        """A fresh directory under ``_work`` for one call's files."""
        name = task or f"t_{secrets.token_hex(8)}"
        path = self.work / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def file_ref(self, path: Path, name: str | None = None) -> dict[str, Any]:
        return {"aep_file": {"path": self.relative(path), "name": name or path.name}}
