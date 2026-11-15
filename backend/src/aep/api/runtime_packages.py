"""Runtime API (``AEP_MODE=runtime``): install packages, switch versions, bind connectors and
see the self-check.

Contract: docs/api/runtime.md (运行包这边的接口).
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, StrictInt

from aep.api.datasources import DataSourceOut, _out
from aep.auth import who
from aep.installs import TOO_BIG, InstallError, NotInstalled, Runtime
from aep.packages import MAX_BYTES

router = APIRouter(prefix="/runtime", tags=["runtime"])
NOT_RUNTIME = "这台机器不是运行包（AEP_MODE=runtime）"


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(404, NOT_RUNTIME)
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


class Activate(BaseModel):
    version: StrictInt = Field(ge=1)


class Bind(BaseModel):
    connector_url: str = Field(min_length=1, max_length=2048)


def _refused(exc: InstallError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def _not_installed(app: str) -> HTTPException:
    return HTTPException(404, f"这台运行包没有装应用 {app}")


@router.post("/packages")
async def install(request: Request, response: Response, runtime: RuntimeDep) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, TOO_BIG)
    try:
        package = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        package = None
    try:
        installed, added = await runtime.install(package, who(request, None))
    except InstallError as exc:
        raise _refused(exc) from exc
    response.status_code = 201 if added else 200
    return installed


@router.get("/apps")
async def list_apps(runtime: RuntimeDep) -> list[dict[str, Any]]:
    return await runtime.all()


@router.get("/apps/{app}")
async def get_app(app: str, runtime: RuntimeDep) -> dict[str, Any]:
    try:
        return await runtime.installed(app)
    except NotInstalled as exc:
        raise _not_installed(app) from exc


@router.post("/apps/{app}/activate")
async def activate(app: str, body: Activate, runtime: RuntimeDep) -> dict[str, Any]:
    try:
        return await runtime.activate(app, body.version)
    except NotInstalled as exc:
        raise _not_installed(app) from exc
    except InstallError as exc:
        raise _refused(exc) from exc


@router.put("/apps/{app}/bindings/{datasource_id}")
async def bind(
    app: str, datasource_id: str, body: Bind, response: Response, runtime: RuntimeDep
) -> DataSourceOut:
    try:
        source, created = await runtime.bind(app, datasource_id, body.connector_url)
    except NotInstalled as exc:
        raise _not_installed(app) from exc
    except InstallError as exc:
        raise _refused(exc) from exc
    response.status_code = 201 if created else 200
    return _out(source)


@router.get("/apps/{app}/check")
async def check(
    app: str, runtime: RuntimeDep, fresh: Annotated[bool, Query()] = False
) -> dict[str, Any]:
    try:
        return await runtime.check(app, fresh=True) if fresh else await runtime.latest(app)
    except NotInstalled as exc:
        raise _not_installed(app) from exc
