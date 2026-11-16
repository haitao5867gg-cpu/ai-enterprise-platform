"""Probing media items with the media processing connector (docs/api/media-processing.md).

Uploads start ``processing`` when the connector is set (``AEP_MEDIA_CONNECTOR_URL``):
the prober calls its ``probe`` tool, writes duration and size, keeps the thumbnail it
made and marks the item ``ready`` (or ``error`` when the file cannot be used). While the
connector cannot be reached, items wait and are tried again a minute later; after a
restart the ones left ``processing`` are probed then.
"""

import asyncio
import logging
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from aep.config import Settings
from aep.media.files import thumbnail_path
from aep.media.store import MediaItem, MediaStore
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError, tool_message

log = logging.getLogger(__name__)

WORK = "_work"
PROBE_TOOL = "probe"
RETRY_SECONDS = 60
BATCH = 20


class WorkFileError(ValueError):
    """A file reference that does not name a file the connector made."""


class ProbeFailed(ValueError):
    """The connector found no usable media in the file; the message says why."""


def work_file(root: Path, path: Any) -> Path:
    """The connector's work file a reference names: under ``_work``, a regular file, no
    links on the way (the connector writes there; nothing may lead out of the volume)."""
    if not isinstance(path, str) or not path:
        raise WorkFileError("文件引用没有路径")
    parts = path.split("/")
    if (
        parts[0] != WORK
        or len(parts) < 2
        or "\\" in path
        or "\x00" in path
        or PurePosixPath(path).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise WorkFileError(f"路径不在连接器的工作目录里：{path}")
    current = root.resolve()
    for part in parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise WorkFileError(f"文件不见了：{path}") from None
        if stat.S_ISLNK(mode):
            raise WorkFileError(f"路径不在连接器的工作目录里：{path}")
    if not stat.S_ISREG(current.lstat().st_mode):
        raise WorkFileError(f"文件不见了：{path}")
    return current


def move(source: Path, target: Path) -> None:
    """Move a file onto the library's disk; across file systems it is copied."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except OSError:
        shutil.copyfile(source, target)
        source.unlink(missing_ok=True)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def _size(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number else None


def measured(found: dict[str, Any]) -> dict[str, Any]:
    """The item fields a probe's answer gives."""
    return {
        "duration_seconds": _number(found.get("duration_seconds")),
        "width": _size(found.get("width")),
        "height": _size(found.get("height")),
    }


def keep_thumbnail(root: Path, item: MediaItem, reference: Any) -> bool:
    """Move the thumbnail a probe made (a work file) to where the item's is kept."""
    ref = reference.get("aep_file") if isinstance(reference, dict) else None
    if not isinstance(ref, dict):
        return False
    try:
        source = work_file(root, ref.get("path"))
        move(source, thumbnail_path(root, item))
    except (WorkFileError, OSError) as exc:
        log.warning("thumbnail of %s not kept: %s", item.item_id, exc)
        return False
    return True


class Prober:
    def __init__(self, store: MediaStore, tools: McpTools | None, settings: Settings) -> None:
        self.store = store
        self.tools = tools
        self.settings = settings
        self._wake = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.media_connector_url) and self.tools is not None

    async def look(self, source: str, thumbnail: bool) -> dict[str, Any] | None:
        """What the connector finds in a file on the media volume; None while it is
        away. Raises ``ProbeFailed`` when the file is not usable media."""
        assert self.tools is not None
        args = {"source": source, "thumbnail": thumbnail}
        try:
            found = await self.tools.call(
                self.settings.media_connector_url,
                PROBE_TOOL,
                args,
                self.settings.tool_timeout_seconds,
            )
        except ConnectorUnavailable as exc:
            log.info("media connector unavailable, %s waits: %s", source, exc)
            return None
        except ToolError as exc:
            raise ProbeFailed(tool_message(exc, PROBE_TOOL)) from None
        if not isinstance(found, dict):
            raise ProbeFailed("连接器的回答格式不对")
        return found

    async def probe(self, item: MediaItem) -> MediaItem | None:
        """Probe one item and record what was found; None while the connector is away.
        An item deleted meanwhile comes back as it was."""
        try:
            found = await self.look(item.rel_path, item.kind != "audio")
        except ProbeFailed as exc:
            return await self._failed(item, str(exc))
        if found is None:
            return None
        root = self.settings.media_dir
        has_thumbnail = await asyncio.to_thread(keep_thumbnail, root, item, found.get("thumbnail"))
        updated = await self.store.update_item(
            item.library_id,
            item.item_id,
            status="ready",
            error=None,
            has_thumbnail=has_thumbnail,
            **measured(found),
        )
        if updated is None and has_thumbnail:  # deleted meanwhile
            thumbnail_path(root, item).unlink(missing_ok=True)
        return updated or item

    async def _failed(self, item: MediaItem, reason: str) -> MediaItem:
        updated = await self.store.update_item(
            item.library_id, item.item_id, status="error", error=f"探测失败：{reason}"
        )
        return updated or item

    def kick(self) -> None:
        """New items to probe: look now rather than at the next round."""
        self._wake.set()

    async def sweep(self) -> None:
        """Probe what is waiting, oldest first, until the connector cannot be reached."""
        while True:
            waiting = await self.store.processing(BATCH)
            for item in waiting:
                if await self.probe(item) is None:
                    return
            if len(waiting) < BATCH:
                return

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 - keep probing later
                log.exception("probing media items failed")
            try:
                await asyncio.wait_for(self._wake.wait(), RETRY_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
