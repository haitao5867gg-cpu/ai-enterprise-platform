"""Apps API: each app's design, configuration versions, draft, eval gate and rollback,
and creating, renaming and archiving flow apps.

Contracts: docs/api/apps-and-skills.md and docs/api/flow-apps.md.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from aep.runtime.appconfig import ConfigError
from aep.runtime.catalog import (
    AppCatalog,
    AppNotFound,
    BadSource,
    EvalUnavailable,
    NotAFlowApp,
    NotPublishable,
)
from aep.runtime.flow.store import AppExists
from aep.runtime.versions import DraftChanged, NoDraft, VersionNotFound

router = APIRouter(prefix="/apps", tags=["apps"])


def get_catalog(request: Request) -> AppCatalog:
    return request.app.state.catalog


Catalog = Annotated[AppCatalog, Depends(get_catalog)]


class SaveDraft(BaseModel):
    config: dict[str, Any]
    actor: str | None = None


class Publish(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    actor: str | None = None


class Activate(BaseModel):
    actor: str | None = None


class Source(BaseModel):
    blank: bool | None = None
    template: str | None = None
    app: str | None = None
    version: int | None = Field(default=None, ge=1)


class CreateApp(BaseModel):
    app: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    title: str = Field(min_length=1, max_length=50)
    summary: str = Field(default="", max_length=500)
    source: Source = Field(default_factory=lambda: Source(blank=True), alias="from")
    actor: str | None = None


class UpdateApp(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=50)
    summary: str | None = Field(default=None, max_length=500)


def _not_found(app: str) -> HTTPException:
    return HTTPException(404, f"没有应用 {app}")


def _invalid(exc: ConfigError) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": ["body", *loc], "msg": message, "type": "value_error"}
            for loc, message in exc.problems
        ]
    )


@router.get("")
async def list_apps(catalog: Catalog) -> list[dict[str, Any]]:
    return await catalog.summaries()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_app(body: CreateApp, catalog: Catalog) -> dict[str, Any]:
    if not body.title.strip():
        raise HTTPException(422, "名称不能为空")
    try:
        await catalog.create(
            body.app,
            body.title.strip(),
            body.summary.strip(),
            body.source.model_dump(exclude_none=True),
            body.actor,
        )
    except AppExists as exc:
        raise HTTPException(409, f"已经有应用 {body.app}") from exc
    except BadSource as exc:
        raise HTTPException(422, str(exc)) from exc
    except ConfigError as exc:
        raise _invalid(exc) from exc
    return await catalog.detail(body.app)


@router.patch("/{app}")
async def update_app(app: str, body: UpdateApp, catalog: Catalog) -> dict[str, Any]:
    try:
        await catalog.update(
            app,
            body.title.strip() if body.title is not None else None,
            body.summary.strip() if body.summary is not None else None,
        )
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except NotAFlowApp as exc:
        raise HTTPException(409, str(exc)) from exc
    return await catalog.detail(app)


@router.delete("/{app}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_app(app: str, catalog: Catalog) -> Response:
    try:
        await catalog.archive(app)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except NotAFlowApp as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{app}")
async def get_app(app: str, catalog: Catalog) -> dict[str, Any]:
    try:
        return await catalog.detail(app)
    except AppNotFound as exc:
        raise _not_found(app) from exc


@router.get("/{app}/versions")
async def list_versions(app: str, catalog: Catalog) -> list[dict[str, Any]]:
    try:
        return await catalog.versions(app)
    except AppNotFound as exc:
        raise _not_found(app) from exc


@router.put("/{app}/draft")
async def save_draft(app: str, body: SaveDraft, catalog: Catalog) -> dict[str, Any]:
    try:
        await catalog.save_draft(app, body.config, body.actor)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except ConfigError as exc:
        raise _invalid(exc) from exc
    return await catalog.detail(app)


@router.delete("/{app}/draft", status_code=status.HTTP_204_NO_CONTENT)
async def discard_draft(app: str, catalog: Catalog) -> Response:
    try:
        await catalog.discard_draft(app)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except NoDraft:
        pass  # Nothing to discard is the state the caller asked for.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{app}/draft/eval", status_code=status.HTTP_202_ACCEPTED)
async def start_eval(app: str, catalog: Catalog) -> dict[str, Any]:
    try:
        await catalog.start_eval(app)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except EvalUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    return await catalog.detail(app)


@router.post("/{app}/draft/publish")
async def publish(app: str, body: Publish, catalog: Catalog) -> dict[str, Any]:
    if not body.note.strip():
        raise HTTPException(422, "发布说明不能为空")
    try:
        await catalog.publish(app, body.note.strip(), body.actor)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except NotPublishable as exc:
        raise HTTPException(409, str(exc)) from exc
    except NoDraft as exc:
        raise HTTPException(409, "没有草稿") from exc
    except DraftChanged as exc:
        raise HTTPException(409, "草稿刚被改过，请刷新后重新评测再发布") from exc
    return await catalog.detail(app)


@router.post("/{app}/versions/{version}/activate")
async def activate(
    app: str, version: int, catalog: Catalog, body: Activate | None = None
) -> dict[str, Any]:
    try:
        await catalog.activate(app, version)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except VersionNotFound as exc:
        raise HTTPException(404, f"应用 {app} 没有版本 {version}") from exc
    return await catalog.detail(app)
