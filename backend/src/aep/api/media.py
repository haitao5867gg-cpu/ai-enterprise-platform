"""Media libraries and their items (docs/api/media.md)."""

import asyncio
import errno
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from aep.api.validation import NoNulls, chinese_errors
from aep.auth import who
from aep.config import Settings, get_settings
from aep.media.files import (
    MAX_FILES,
    UploadError,
    media_type,
    remove_files,
    remove_library,
    save_files,
    thumbnail_path,
    validate_files,
)
from aep.media.probe import Prober
from aep.media.store import (
    DuplicateName,
    ItemLimit,
    ItemQuery,
    MediaItem,
    MediaKind,
    MediaLibrary,
    MediaStore,
    MissingLibrary,
)

LABELS = {
    "name": "名称",
    "description": "说明",
    "tags": "标签",
    "usable": "可用",
    "uploaded_by": "上传人",
    "q": "搜索词",
    "limit": "每页条数",
    "offset": "起始位置",
}
router = APIRouter(prefix="/media-libraries", tags=["media"], route_class=chinese_errors(LABELS))


def get_store(request: Request) -> MediaStore:
    return request.app.state.media


def get_prober(request: Request) -> Prober | None:
    """Probes uploads when the media processing connector is set; None otherwise."""
    prober = getattr(request.app.state, "media_prober", None)
    return prober if prober is not None and prober.enabled else None


Store = Annotated[MediaStore, Depends(get_store)]
Config = Annotated[Settings, Depends(get_settings)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
Tags = Annotated[list[Tag], Field(max_length=20)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Link = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
TAGS_FORM = "「标签」要是 JSON 数组，最多 20 个，每个 1–20 个字"


def _unique(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tags))


class NewLibrary(NoNulls):
    name: Name
    description: Description = ""


class LibraryChanges(NoNulls):
    name: Name | None = None
    description: Description | None = None


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["upload", "stock", "run"]
    site: Text | None = None
    url: Link | None = None
    author: Text | None = None
    license: Text | None = None
    license_url: Link | None = None
    run_id: Text | None = None

    @field_validator("url", "license_url")
    @classmethod
    def web_address(cls, value: str | None) -> str | None:
        # Pages show these as links; only web addresses.
        if value and not value.lower().startswith(("https://", "http://")):
            raise ValueError("来源链接要以 http:// 或 https:// 开头")
        return value or None


class ItemChanges(NoNulls):
    description: Description | None = None
    tags: Tags | None = None
    usable: bool | None = None
    source: Source | None = None

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, value: list[str] | None) -> list[str] | None:
        return _unique(value) if value is not None else None


class LibraryOut(BaseModel):
    library_id: str
    name: str
    description: str
    item_count: int
    total_bytes: int
    status_counts: dict[str, int]
    created_at: datetime
    updated_at: datetime


class ItemOut(BaseModel):
    item_id: str
    library_id: str
    name: str
    kind: MediaKind
    size_bytes: int
    status: Literal["processing", "ready", "error"]
    error: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    orientation: Literal["portrait", "landscape", "square"] | None
    description: str
    tags: list[str]
    usable: bool
    source: Source
    uploaded_by: str | None
    has_thumbnail: bool
    created_at: datetime
    updated_at: datetime


class ItemPage(BaseModel):
    items: list[ItemOut]
    total: int


def library_out(library: MediaLibrary) -> LibraryOut:
    return LibraryOut(
        library_id=library.library_id,
        name=library.name,
        description=library.description,
        item_count=library.item_count,
        total_bytes=library.total_bytes,
        status_counts=library.status_counts,
        created_at=library.created_at,
        updated_at=library.updated_at,
    )


def _orientation(item: MediaItem) -> Literal["portrait", "landscape", "square"] | None:
    if not item.width or not item.height:
        return None
    if item.width == item.height:
        return "square"
    return "landscape" if item.width > item.height else "portrait"


def _source(source: dict[str, Any]) -> Source:
    fields = Source.model_fields
    return Source.model_construct(**{k: source.get(k) for k in fields})


def item_out(item: MediaItem) -> ItemOut:
    return ItemOut(
        item_id=item.item_id,
        library_id=item.library_id,
        name=item.name,
        kind=item.kind,
        size_bytes=item.size_bytes,
        status=item.status,
        error=item.error,
        duration_seconds=item.duration_seconds,
        width=item.width,
        height=item.height,
        orientation=_orientation(item),
        description=item.description,
        tags=list(item.tags),
        usable=item.usable,
        source=_source(item.source),
        uploaded_by=item.uploaded_by,
        has_thumbnail=item.has_thumbnail,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def _library(store: MediaStore, library_id: str) -> MediaLibrary:
    library = await store.library(library_id)
    if library is None:
        raise HTTPException(404, "没有这个媒体库")
    return library


async def _item(store: MediaStore, library_id: str, item_id: str) -> MediaItem:
    await _library(store, library_id)
    item = await store.item(library_id, item_id)
    if item is None:
        raise HTTPException(404, "没有这个素材")
    return item


def _form_tags(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ()
    try:
        tags = TypeAdapter(Tags).validate_python(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, TAGS_FORM) from exc
    return tuple(_unique(tags))


@router.get("")
async def list_libraries(store: Store) -> list[LibraryOut]:
    return [library_out(library) for library in await store.libraries()]


@router.post("", status_code=201)
async def create_library(body: NewLibrary, store: Store) -> LibraryOut:
    try:
        library = await store.create_library(body.name, body.description)
    except DuplicateName as exc:
        raise HTTPException(409, f"已存在名为「{body.name}」的媒体库") from exc
    return library_out(library)


@router.get("/{library_id}")
async def get_library(library_id: str, store: Store) -> LibraryOut:
    return library_out(await _library(store, library_id))


@router.patch("/{library_id}")
async def update_library(library_id: str, body: LibraryChanges, store: Store) -> LibraryOut:
    changes = body.model_dump(exclude_unset=True)
    try:
        library = await store.update_library(library_id, **changes)
    except DuplicateName as exc:
        raise HTTPException(409, f"已存在名为「{body.name}」的媒体库") from exc
    if library is None:
        raise HTTPException(404, "没有这个媒体库")
    return library_out(library)


@router.delete("/{library_id}", status_code=204)
async def delete_library(library_id: str, store: Store, settings: Config) -> Response:
    library = await _library(store, library_id)
    await store.delete_library(library.library_id)
    await asyncio.to_thread(remove_library, library.library_id, settings.media_dir)
    return Response(status_code=204)


@router.get("/{library_id}/items")
async def list_items(
    library_id: str,
    store: Store,
    q: Annotated[str, Query(max_length=200)] = "",
    tag: Annotated[str | None, Query(max_length=20)] = None,
    kind: MediaKind | None = None,
    usable: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ItemPage:
    await _library(store, library_id)
    query = ItemQuery(
        q=q, tag=(tag or "").strip() or None, kind=kind, usable=usable, limit=limit, offset=offset
    )
    items, total = await store.items(library_id, query)
    return ItemPage(items=[item_out(item) for item in items], total=total)


@router.post("/{library_id}/items", status_code=202)
async def upload_items(
    library_id: str,
    store: Store,
    settings: Config,
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
    description: Annotated[str | None, Form(max_length=500)] = None,
    tags: Annotated[str | None, Form()] = None,
    uploaded_by: Annotated[str | None, Form(max_length=50)] = None,
) -> list[ItemOut]:
    library = await _library(store, library_id)
    return await upload(
        store,
        settings,
        library,
        files,
        description,
        tags,
        who(request, uploaded_by),
        get_prober(request),
    )


async def upload(
    store: MediaStore,
    settings: Settings,
    library: MediaLibrary,
    files: list[UploadFile] | None,
    description: str | None,
    tags: str | None,
    uploaded_by: str | None,
    prober: Prober | None = None,
) -> list[ItemOut]:
    """Check a whole batch, then save it into the library (also the app API's upload).
    With a ``prober`` the items start ``processing`` and are probed in the background."""
    if not files:
        raise HTTPException(422, "请选择至少一个视频、音频或图片文件")
    if len(files) > MAX_FILES:
        raise HTTPException(422, "单次最多上传 20 个文件")
    labels = _form_tags(tags)
    limit = settings.media_max_items
    if library.item_count + len(files) > limit:
        raise HTTPException(422, f"一个媒体库最多 {limit} 个素材")
    try:
        items = await asyncio.to_thread(
            validate_files,
            files,
            library.library_id,
            settings.media_max_file_mb,
            description=(description or "").strip(),
            tags=labels,
            uploaded_by=uploaded_by,
            status="processing" if prober is not None else "ready",
        )
    except UploadError as exc:
        raise HTTPException(422, str(exc)) from exc
    registered = False
    try:
        await asyncio.to_thread(save_files, files, items, settings.media_dir)
        added = await store.add_items(library.library_id, items, limit)
        registered = True
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise HTTPException(507, "服务器硬盘满了，这批素材没有保存") from exc
        raise
    except ItemLimit as exc:
        raise HTTPException(422, str(exc)) from exc
    except MissingLibrary as exc:
        raise HTTPException(404, "没有这个媒体库") from exc
    finally:
        if not registered:
            await asyncio.to_thread(remove_files, items, settings.media_dir)
    if prober is not None:
        prober.kick()
    return [item_out(item) for item in added]


@router.get("/{library_id}/items/{item_id}")
async def get_item(library_id: str, item_id: str, store: Store) -> ItemOut:
    return item_out(await _item(store, library_id, item_id))


@router.patch("/{library_id}/items/{item_id}")
async def update_item(library_id: str, item_id: str, body: ItemChanges, store: Store) -> ItemOut:
    await _item(store, library_id, item_id)
    changes = body.model_dump(exclude_unset=True)
    if body.source is not None:
        # The source is replaced whole, every field present.
        changes["source"] = body.source.model_dump()
    item = await store.update_item(library_id, item_id, **changes)
    if item is None:
        raise HTTPException(404, "没有这个素材")
    return item_out(item)


@router.delete("/{library_id}/items/{item_id}", status_code=204)
async def delete_item(library_id: str, item_id: str, store: Store, settings: Config) -> Response:
    await _item(store, library_id, item_id)
    item = await store.delete_item(library_id, item_id)
    if item is not None:
        await asyncio.to_thread(remove_files, [item], settings.media_dir)
    return Response(status_code=204)


@router.get("/{library_id}/items/{item_id}/file")
async def item_file(library_id: str, item_id: str, store: Store, settings: Config) -> FileResponse:
    item = await _item(store, library_id, item_id)
    path = settings.media_dir / item.rel_path
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "素材文件不见了")
    return FileResponse(
        path,
        media_type=media_type(item.name),
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(item.name, safe='')}"},
    )


@router.get("/{library_id}/items/{item_id}/thumbnail")
async def item_thumbnail(
    library_id: str, item_id: str, store: Store, settings: Config
) -> FileResponse:
    item = await _item(store, library_id, item_id)
    path = thumbnail_path(settings.media_dir, item)
    if not item.has_thumbnail or not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "这个素材没有缩略图")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})
