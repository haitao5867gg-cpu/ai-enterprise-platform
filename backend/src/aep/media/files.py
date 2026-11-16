"""Media files on the platform's disk: check a whole upload batch before saving any of it.

Files live under ``AEP_MEDIA_DIR`` at ``<library_id>/<item_id>/<name>``. A batch is
checked from the uploaded temporary files without reading them into memory (a video
may be a gigabyte).
"""

import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, cast

from fastapi import UploadFile

from aep.media.store import ItemStatus, MediaItem, MediaKind

MAX_FILES = 20
# Thumbnails sit in this folder of each library, named by item id.
THUMBNAILS = "_thumbnails"
# A file name's UTF-8 bytes; leaves room under the usual 255-byte limit.
MAX_NAME_BYTES = 200
COPY_BUFFER = 1024 * 1024

VIDEO = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
AUDIO = (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac")
IMAGE = (".jpg", ".jpeg", ".png", ".webp")
KINDS: dict[str, MediaKind] = {
    **dict.fromkeys(VIDEO, cast(MediaKind, "video")),
    **dict.fromkeys(AUDIO, cast(MediaKind, "audio")),
    **dict.fromkeys(IMAGE, cast(MediaKind, "image")),
}
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
UNSUPPORTED = (
    "文件「{file}」：只支持视频（mp4、mov、m4v、webm、mkv）、"
    "音频（m4a、mp3、wav、aac、ogg、opus、flac）和图片（jpg、jpeg、png、webp）"
)


class UploadError(ValueError):
    pass


def media_type(name: str) -> str:
    return MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _short(name: str) -> str:
    """The name, its stem cut so the whole fits in MAX_NAME_BYTES."""
    if len(name.encode()) <= MAX_NAME_BYTES:
        return name
    path = Path(name)
    room = MAX_NAME_BYTES - len(path.suffix.encode())
    stem = path.stem.encode()[:room].decode(errors="ignore")
    return stem + path.suffix


def _size(upload: UploadFile) -> int:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def validate_files(
    files: list[UploadFile],
    library_id: str,
    max_file_mb: int,
    *,
    description: str = "",
    tags: tuple[str, ...] = (),
    uploaded_by: str | None = None,
    status: ItemStatus = "ready",
) -> list[MediaItem]:
    """One item per file, or UploadError for the first file that cannot be kept."""
    items: list[MediaItem] = []
    for upload in files:
        original = upload.filename or "未命名文件"
        name = Path(original.replace("\\", "/")).name
        if "\x00" in name:
            raise UploadError(f"文件「{original}」：文件名含有无效字符")
        kind = KINDS.get(Path(name).suffix.lower())
        if kind is None:
            raise UploadError(UNSUPPORTED.format(file=original))
        size = _size(upload)
        if size > max_file_mb * 1024 * 1024:
            raise UploadError(f"文件「{original}」：单个文件不能超过 {max_file_mb} MB")
        if size == 0:
            raise UploadError(f"文件「{original}」：是空文件")
        name = _short(name)
        item_id = str(uuid.uuid4())
        items.append(
            MediaItem(
                item_id=item_id,
                library_id=library_id,
                name=name,
                kind=kind,
                size_bytes=size,
                rel_path=f"{library_id}/{item_id}/{name}",
                status=status,
                description=description,
                description_by="person" if description else None,
                tags=tags,
                uploaded_by=uploaded_by,
            )
        )
    return items


def save_files(files: list[UploadFile], items: list[MediaItem], root: Path) -> None:
    for upload, item in zip(files, items, strict=True):
        path = root / item.rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        upload.file.seek(0)
        with path.open("wb") as target:
            shutil.copyfileobj(upload.file, target, COPY_BUFFER)


class VolumeFileError(ValueError):
    """A media path that does not name a file on the media volume."""


def volume_file(root: Path, path: Any, outside: str = "文件不在媒体库里") -> Path:
    """The file a media path (relative to ``root``, docs/api/media-processing.md) names: no
    absolute path, ``..`` or link on the way, and a regular file at the end."""
    if not isinstance(path, str) or not path:
        raise VolumeFileError("没有文件路径")
    parts = path.split("/")
    if (
        "\\" in path
        or "\x00" in path
        or PurePosixPath(path).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise VolumeFileError(f"{outside}：{path}")
    current = root.resolve()
    for part in parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise VolumeFileError(f"文件不见了：{path}") from None
        if stat.S_ISLNK(mode):
            raise VolumeFileError(f"{outside}：{path}")
    if not stat.S_ISREG(current.lstat().st_mode):
        raise VolumeFileError(f"文件不见了：{path}")
    return current


def media_path(value: Any) -> str | None:
    """The media path a value names: a path, an item's summary (``path``) or a file
    reference (``{"aep_file": {"path"}}``); None for anything else."""
    if isinstance(value, dict) and set(value) == {"aep_file"}:
        value = value["aep_file"]
    if isinstance(value, dict):
        value = value.get("path")
    return value if isinstance(value, str) and value.strip() else None


def thumbnail_path(root: Path, item: MediaItem) -> Path:
    """Where an item's thumbnail is kept: beside the library's item folders."""
    return root / item.library_id / THUMBNAILS / f"{item.item_id}.jpg"


def remove_files(items: list[MediaItem], root: Path) -> None:
    for item in items:
        shutil.rmtree((root / item.rel_path).parent, ignore_errors=True)
        thumbnail_path(root, item).unlink(missing_ok=True)


def remove_library(library_id: str, root: Path) -> None:
    shutil.rmtree(root / library_id, ignore_errors=True)
