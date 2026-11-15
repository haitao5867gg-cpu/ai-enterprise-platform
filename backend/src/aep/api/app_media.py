"""The app API's media endpoints (``/v1/apps/{app}/media-libraries``): an app's keys
list, upload, change and download items of the libraries the app's current version
binds (docs/api/media.md, "应用用媒体库"). Each call goes on to the media API's own
handlers once the binding allows it.
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse

from aep.api import media
from aep.api.app_access import api_key, run_service
from aep.api.media import LABELS, Config, ItemChanges, ItemOut, ItemPage, LibraryOut
from aep.api.validation import chinese_errors
from aep.entries import CLOSED_ENTRY
from aep.media.store import MediaKind, MediaLibrary, MediaStore
from aep.runtime.flow import media as flowmedia

router = APIRouter(
    prefix="/v1/apps/{app}/media-libraries", tags=["app access"], route_class=chinese_errors(LABELS)
)


class BoundLibrary(LibraryOut):
    access: flowmedia.Access


def _store(request: Request) -> MediaStore:
    # The store runs read their media inputs from.
    return run_service(request).media


async def _bindings(request: Request, app: str) -> list[dict[str, str]]:
    """The libraries the key's app binds, once the key may use the app API."""
    key = await api_key(request)
    if key.app != app:
        raise HTTPException(403, f"这个密钥不能调用应用 {app}")
    service = run_service(request)
    if not (await service.entries.get(app)).open("api"):
        raise HTTPException(403, CLOSED_ENTRY)
    return flowmedia.bindings(await service.current_config(app) or {})


async def _bound(request: Request, app: str, library_id: str, write: bool = False) -> MediaLibrary:
    access = next(
        (b["access"] for b in await _bindings(request, app) if b["library_id"] == library_id),
        None,
    )
    library = await _store(request).library(library_id) if access else None
    if library is None:
        raise HTTPException(404, "这个应用没有绑定这个媒体库")
    if write and access != "write":
        raise HTTPException(403, f"这个应用对媒体库「{library.name}」只有读权限")
    return library


@router.get("")
async def list_libraries(app: str, request: Request) -> list[BoundLibrary]:
    store = _store(request)
    found: list[BoundLibrary] = []
    for bound in await _bindings(request, app):
        library = await store.library(bound["library_id"])
        if library is not None:
            out = media.library_out(library)
            found.append(BoundLibrary(**out.model_dump(), access=bound["access"]))
    return found


@router.get("/{library_id}/items")
async def list_items(
    app: str,
    library_id: str,
    request: Request,
    q: Annotated[str, Query(max_length=200)] = "",
    tag: Annotated[str | None, Query(max_length=20)] = None,
    kind: MediaKind | None = None,
    usable: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ItemPage:
    await _bound(request, app, library_id)
    return await media.list_items(library_id, _store(request), q, tag, kind, usable, limit, offset)


@router.post("/{library_id}/items", status_code=202)
async def upload_items(
    app: str,
    library_id: str,
    request: Request,
    settings: Config,
    files: Annotated[list[UploadFile] | None, File()] = None,
    description: Annotated[str | None, Form(max_length=500)] = None,
    tags: Annotated[str | None, Form()] = None,
) -> list[ItemOut]:
    library = await _bound(request, app, library_id, write=True)
    name = request.state.api_key.name
    return await media.upload(
        _store(request),
        settings,
        library,
        files,
        description,
        tags,
        f"API：{name}",
        media.get_prober(request),
    )


@router.get("/{library_id}/items/{item_id}")
async def get_item(app: str, library_id: str, item_id: str, request: Request) -> ItemOut:
    await _bound(request, app, library_id)
    return await media.get_item(library_id, item_id, _store(request))


@router.patch("/{library_id}/items/{item_id}")
async def update_item(
    app: str, library_id: str, item_id: str, body: ItemChanges, request: Request
) -> ItemOut:
    await _bound(request, app, library_id, write=True)
    return await media.update_item(library_id, item_id, body, _store(request))


@router.delete("/{library_id}/items/{item_id}", status_code=204)
async def delete_item(
    app: str, library_id: str, item_id: str, request: Request, settings: Config
) -> Response:
    await _bound(request, app, library_id, write=True)
    return await media.delete_item(library_id, item_id, _store(request), settings)


@router.get("/{library_id}/items/{item_id}/file")
async def item_file(
    app: str, library_id: str, item_id: str, request: Request, settings: Config
) -> FileResponse:
    await _bound(request, app, library_id)
    return await media.item_file(library_id, item_id, _store(request), settings)


@router.get("/{library_id}/items/{item_id}/thumbnail")
async def item_thumbnail(
    app: str, library_id: str, item_id: str, request: Request, settings: Config
) -> FileResponse:
    await _bound(request, app, library_id)
    return await media.item_thumbnail(library_id, item_id, _store(request), settings)
