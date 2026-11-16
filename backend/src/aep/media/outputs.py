"""Files a run's tool made, saved into a media library (docs/api/media-processing.md,
"产出的文件存进媒体库").

A connector writes what it makes under ``_work`` on the media volume and names each file
in its result as ``{"aep_file": {"path", "name"}}``. A tool node with ``save_media``
probes every such file, moves it into the library, records it as made by the run
(``source.kind: run``) and puts the item's summary where the reference was, so later
nodes, the run's ``files`` and the app API see the library's file.
"""

import asyncio
import dataclasses
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aep.config import Settings
from aep.media.files import KINDS, UNSUPPORTED, _short, thumbnail_path
from aep.media.probe import (
    ProbeFailed,
    Prober,
    WorkFileError,
    keep_thumbnail,
    measured,
    move,
    work_file,
)
from aep.media.store import ItemLimit, MediaItem, MediaStore, MissingLibrary

MAX_SAVED = 20
FILE_KEY = "aep_file"


class SaveError(ValueError):
    """Why the files were not saved; none of them were."""


def is_file_ref(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {FILE_KEY} and isinstance(value[FILE_KEY], dict)
    )


def file_refs(value: Any) -> list[dict[str, Any]]:
    """Every file reference in a tool's result, in order."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if is_file_ref(node):
            found.append(node[FILE_KEY])
        elif isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def replace_refs(value: Any, saved: dict[str, dict[str, Any]]) -> Any:
    """The result with each saved file's reference turned into its item's summary."""
    if is_file_ref(value):
        path = value[FILE_KEY].get("path")
        return saved.get(path, value) if isinstance(path, str) else value
    if isinstance(value, dict):
        return {key: replace_refs(child, saved) for key, child in value.items()}
    if isinstance(value, list):
        return [replace_refs(child, saved) for child in value]
    return value


def import_key(run_id: str, path: str) -> str:
    return hashlib.sha256(f"{run_id}\n{path}".encode()).hexdigest()


@dataclass
class _Planned:
    path: str
    source: Path
    item: MediaItem
    thumbnail: Any = None  # the probe's thumbnail, a work file


def _name(ref: dict[str, Any], path: str) -> str:
    raw = ref.get("name")
    name = raw if isinstance(raw, str) and raw.strip() else path.rsplit("/", 1)[-1]
    return Path(name.replace("\\", "/")).name.strip()


async def save_outputs(
    value: Any,
    *,
    store: MediaStore,
    settings: Settings,
    prober: Prober | None,
    library_id: str,
    run_id: str,
    description: str = "",
    tags: tuple[str, ...] = (),
    summary: Callable[[MediaItem], dict[str, Any]],
) -> Any:
    """Save the files ``value`` names into the library and return it with their
    summaries in place; a file already saved by this run is not saved again."""
    refs = file_refs(value)
    if not refs:
        return value
    if len(refs) > MAX_SAVED:
        raise SaveError(f"一次最多存 {MAX_SAVED} 个文件，工具产出了 {len(refs)} 个")
    library = await store.library(library_id)
    if library is None:
        raise SaveError("没有这个媒体库")
    root = settings.media_dir
    saved: dict[str, MediaItem] = {}
    planned: list[_Planned] = []
    for ref in refs:
        path = ref.get("path")
        shown = _name(ref, path) if isinstance(path, str) else "?"
        if isinstance(path, str) and (path in saved or any(p.path == path for p in planned)):
            continue
        key = import_key(run_id, path) if isinstance(path, str) else ""
        if isinstance(path, str) and (done := await store.imported(library_id, key)):
            saved[path] = await _recovered(done, root, path, store, prober)
            continue
        try:
            source = await asyncio.to_thread(work_file, root, path)
        except WorkFileError as exc:
            raise SaveError(f"文件「{shown}」：{exc}") from None
        assert isinstance(path, str)
        name = _short(shown) if shown and "\x00" not in shown else ""
        kind = KINDS.get(Path(name).suffix.lower()) if name else None
        if kind is None:
            raise SaveError(UNSUPPORTED.format(file=shown or path))
        size = (await asyncio.to_thread(source.stat)).st_size
        if size == 0:
            raise SaveError(f"文件「{name}」：是空文件")
        if size > settings.media_max_file_mb * 1024 * 1024:
            raise SaveError(f"文件「{name}」：单个文件不能超过 {settings.media_max_file_mb} MB")
        item_id = str(uuid.uuid4())
        item = MediaItem(
            item_id=item_id,
            library_id=library_id,
            name=name,
            kind=kind,
            size_bytes=size,
            rel_path=f"{library_id}/{item_id}/{name}",
            status="ready",
            description=description,
            description_by="person" if description else None,
            tags=tags,
            source={"kind": "run", "run_id": run_id},
            import_key=key,
        )
        planned.append(_Planned(path, source, item))
    if planned and library.item_count + len(planned) > settings.media_max_items:
        raise SaveError(f"一个媒体库最多 {settings.media_max_items} 个素材")
    if prober is not None:
        await _probe_all(prober, planned)
    moved: list[_Planned] = []
    moving = asyncio.ensure_future(asyncio.to_thread(_move_all, root, planned, moved))
    try:
        await asyncio.shield(moving)
        added = await store.add_items(
            library_id, [plan.item for plan in planned], settings.media_max_items
        )
    except BaseException as exc:
        # Whatever stopped it (a full library, a lost database, a cancel), put back what
        # was moved, so a retry finds the files where the tool left them. A cancel stops
        # the wait, not the thread moving the files: it is let finish first, so every
        # moved file is known. Unless the records were written after all: then the files
        # belong where they are.
        cleanup = _Cleanup()
        await cleanup.settle(moving)
        if not (moved and await cleanup.registered(store, library_id, moved[0].item.import_key)):
            _put_back(root, moved)
        if cleanup.cancelled and not isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError() from exc
        if not isinstance(exc, OSError | ItemLimit | MissingLibrary):
            raise
        if isinstance(exc, MissingLibrary):
            raise SaveError("没有这个媒体库") from exc
        if isinstance(exc, ItemLimit):
            raise SaveError(str(exc)) from exc
        raise SaveError(f"移动文件失败：{exc}") from exc
    for plan, item in zip(planned, added, strict=True):
        saved[plan.path] = item
    return replace_refs(value, {path: summary(item) for path, item in saved.items()})


async def _probe_all(prober: Prober, planned: list[_Planned]) -> None:
    """Probe each file where the tool left it, before anything is saved: a file that is
    not usable media stops the batch. While the connector is away the files are saved
    as they are (as an upload is then)."""
    for plan in planned:
        item = plan.item
        try:
            found = await prober.look(plan.path, item.kind != "audio")
        except ProbeFailed as exc:
            raise SaveError(f"文件「{item.name}」：探测失败：{exc}") from None
        if found is None:
            return
        plan.item = dataclasses.replace(item, **measured(found))
        plan.thumbnail = found.get("thumbnail")


async def _recovered(
    done: MediaItem, root: Path, path: str, store: MediaStore, prober: Prober | None
) -> MediaItem:
    """An item this run saved before. When its records were written but the answer was
    lost (and the check after it too), its file was put back in ``_work`` for a retry:
    it is brought in now and probed again, as a probe meanwhile found no file."""
    target = root / done.rel_path
    if done.status == "error":
        raise SaveError(f"文件「{done.name}」：{done.error or '用不了'}")
    if await asyncio.to_thread(target.is_file):
        return done
    try:
        source = await asyncio.to_thread(work_file, root, path)
    except WorkFileError:
        raise SaveError(f"文件「{done.name}」：文件不见了") from None
    try:
        await asyncio.to_thread(move, source, target)
    except OSError as exc:
        raise SaveError(f"移动文件失败：{exc}") from exc
    if prober is None:
        return done
    probed = await prober.probe(done)  # its thumbnail went when the file was put back
    if probed is None:  # the connector is away: usable as it is
        probed = await store.update_item(done.library_id, done.item_id, has_thumbnail=False)
    elif probed.status == "error":
        raise SaveError(f"文件「{done.name}」：{probed.error}")
    return probed or done


def _move_all(root: Path, planned: list[_Planned], moved: list[_Planned]) -> None:
    """In a worker thread: each file into the library, noted in ``moved`` once it is,
    and its thumbnail where the item's is kept."""
    for plan in planned:
        move(plan.source, root / plan.item.rel_path)
        moved.append(plan)
        if plan.thumbnail is not None:
            kept = keep_thumbnail(root, plan.item, plan.thumbnail)
            plan.item = dataclasses.replace(plan.item, has_thumbnail=kept)


class _Cleanup:
    """Waits a cancel does not cut short; a cancel that comes meanwhile is noted, to be
    passed on once the files are where they belong."""

    def __init__(self) -> None:
        self.cancelled = False

    async def settle(self, future: "asyncio.Future[Any]") -> None:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                if not future.cancelled():
                    self.cancelled = True
            except BaseException:  # noqa: BLE001 - its outcome is the caller's to read
                pass

    async def registered(self, store: MediaStore, library_id: str, key: str) -> bool:
        """Whether the batch's records were written (they are written together); not
        known counts as not written."""
        lookup = asyncio.ensure_future(asyncio.wait_for(store.imported(library_id, key), 5))
        await self.settle(lookup)
        if lookup.cancelled() or lookup.exception() is not None:
            return False
        return lookup.result() is not None


def _put_back(root: Path, moved: list["_Planned"]) -> None:
    """Renames on the media volume: done at once, even while a cancel is handled. The
    thumbnails go; a retry probes again."""
    for plan in moved:
        try:
            thumbnail_path(root, plan.item).unlink(missing_ok=True)
        except OSError:
            pass
        target = root / plan.item.rel_path
        try:
            move(target, plan.source)
        except OSError:
            continue
        _drop_folder(target.parent)


def _drop_folder(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass
