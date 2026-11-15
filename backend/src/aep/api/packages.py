"""Packages API on the platform: what a package of an app needs, and the signed package
itself, for an external app to run on its own machine.

Contract: docs/api/runtime.md (运行要求清单, 流程包).
"""

import json
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictInt

from aep.config import Settings, get_settings
from aep.packages import (
    BAD_HOST,
    MAX_HOSTS,
    MAX_NOTES,
    NO_SIGNING_KEY,
    Packages,
    RuntimeNeeds,
    Signer,
    Unsupported,
    hosts,
)
from aep.runtime.catalog import AppNotFound
from aep.runtime.versions import VersionNotFound

router = APIRouter(tags=["packages"])


def get_packages(request: Request) -> Packages:
    return request.app.state.packages


PackagesDep = Annotated[Packages, Depends(get_packages)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class Machine(BaseModel):
    cpu_cores: StrictInt = Field(ge=1, le=256)
    memory_gb: StrictInt = Field(ge=1, le=4096)
    disk_gb: StrictInt = Field(ge=1, le=100000)


class SetNeeds(BaseModel):
    machine: Machine | None = None
    network: list[str] = Field(default_factory=list, max_length=MAX_HOSTS)
    notes: str = Field(default="", max_length=MAX_NOTES)


def _signer(settings: Settings) -> Signer:
    signer = Signer.from_text(settings.package_signing_key)
    if signer is None:
        raise HTTPException(503, NO_SIGNING_KEY)
    return signer


def _not_found(app: str) -> HTTPException:
    return HTTPException(404, f"没有应用 {app}")


@router.get("/apps/{app}/runtime-requirements")
async def get_needs(app: str, packages: PackagesDep) -> dict[str, Any]:
    try:
        return (await packages.needs(app)).as_dict()
    except AppNotFound as exc:
        raise _not_found(app) from exc


@router.put("/apps/{app}/runtime-requirements")
async def set_needs(app: str, body: SetNeeds, packages: PackagesDep) -> dict[str, Any]:
    try:
        network = hosts(body.network)
    except ValueError as exc:
        raise HTTPException(422, BAD_HOST) from exc
    needs = RuntimeNeeds(
        body.machine.model_dump() if body.machine is not None else None, network, body.notes
    )
    try:
        return (await packages.set_needs(app, needs)).as_dict()
    except AppNotFound as exc:
        raise _not_found(app) from exc


@router.get("/apps/{app}/versions/{version}/requirements")
async def requirements(app: str, version: int, packages: PackagesDep) -> dict[str, Any]:
    try:
        return await packages.requirements(app, version)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except VersionNotFound as exc:
        raise HTTPException(404, f"应用 {app} 没有版本 {version}") from exc


@router.get("/apps/{app}/versions/{version}/package")
async def package(app: str, version: int, packages: PackagesDep, settings: SettingsDep) -> Response:
    signer = _signer(settings)
    try:
        built = await packages.build(app, version, signer)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except VersionNotFound as exc:
        raise HTTPException(404, f"应用 {app} 没有版本 {version}") from exc
    except Unsupported as exc:
        raise HTTPException(422, str(exc)) from exc
    name = quote(f"{app}-v{version}.aep-package.json", safe="")
    return Response(
        json.dumps(built, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@router.get("/packages/public-key")
async def public_key(settings: SettingsDep) -> dict[str, str]:
    signer = _signer(settings)
    return {"key_id": signer.key_id, "public_key": signer.public_key}
