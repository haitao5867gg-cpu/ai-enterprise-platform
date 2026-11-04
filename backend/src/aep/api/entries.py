"""The ways into an app, each of which can be closed (docs/api/app-access.md, "入口")."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from aep.auth import require, who
from aep.entries import CLOSED_TO_USERS, ENTRIES
from aep.runtime.service import RunService

router = APIRouter(prefix="/apps", tags=["apps"])


class Entry(BaseModel):
    enabled: bool
    last_used_at: datetime | None


class AppAccess(BaseModel):
    use: Entry
    api: Entry
    mcp: Entry
    apps: Entry


class SetAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use: bool | None = None
    api: bool | None = None
    mcp: bool | None = None
    apps: bool | None = None


def _service(request: Request) -> RunService:
    return request.app.state.run_service


async def open_to_users(request: Request, app: str) -> None:
    """403 for someone who is not a builder when the app's use entry is closed: builders
    and admins still run it, to try it out."""
    if not (await _service(request).entries.get(app)).use:
        require(request, "builder", CLOSED_TO_USERS)


async def _access(service: RunService, app: str) -> AppAccess:
    found = await service.entries.get(app)
    used = await service.last_used(app)
    return AppAccess(**{e: Entry(enabled=found.open(e), last_used_at=used.get(e)) for e in ENTRIES})


async def _app(service: RunService, app: str) -> None:
    if await service.kind(app) is None:
        raise HTTPException(404, f"没有应用 {app}")


@router.get("/{app}/access")
async def get_access(app: str, request: Request) -> AppAccess:
    service = _service(request)
    await _app(service, app)
    return await _access(service, app)


@router.put("/{app}/access")
async def set_access(app: str, body: SetAccess, request: Request) -> AppAccess:
    service = _service(request)
    await _app(service, app)
    found = await service.entries.get(app)
    for entry, value in body.model_dump(exclude_none=True).items():
        setattr(found, entry, value)
    found.updated_by, found.updated_at = who(request, None), datetime.now(UTC)
    await service.entries.put(found)
    return await _access(service, app)
