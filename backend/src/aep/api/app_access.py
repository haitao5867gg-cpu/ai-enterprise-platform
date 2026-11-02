"""API keys and the app API (``/v1``). Contract: docs/api/app-access.md."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StringConstraints

from aep.api.runs import ProjectChoice
from aep.api.validation import NoNulls, chinese_errors
from aep.app_access import ApiKey, ApiKeys, TooManyKeys, bearer, run_result, settle
from aep.auth import who
from aep.runtime.service import (
    InvalidValues,
    ModelNotAllowed,
    NotWaiting,
    ProjectProblem,
    RunNotFound,
    RunService,
    UnknownApp,
    UnknownVersion,
)

router = APIRouter(tags=["app access"], route_class=chinese_errors({"name": "名称"}))

Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50, pattern=r"^[^\x00]*$"),
]
Wait = Annotated[int, Field(ge=0, le=60)]


class ApiKeyOut(BaseModel):
    key_id: str
    app: str
    name: str
    prefix: str
    created_by: str | None
    created_at: datetime
    last_used_at: datetime | None


class NewApiKey(ApiKeyOut):
    secret: str


class CreateKey(NoNulls):
    name: Name


class Pending(BaseModel):
    kind: str | None
    prompt: str
    fields: list[dict[str, Any]] | None


class RunResult(BaseModel):
    run_id: str
    app: str
    status: str
    result: dict[str, Any] | None
    pending: Pending | None
    error: str | None
    link: str
    created_at: datetime
    updated_at: datetime


class StartRun(NoNulls):
    input: dict[str, Any] = Field(default_factory=dict)
    wait: Wait = 0
    project: ProjectChoice | None = None


class Answer(NoNulls):
    values: dict[str, Any] = Field(default_factory=dict)
    wait: Wait = 0


def _service(request: Request) -> RunService:
    return request.app.state.run_service


def _keys(request: Request) -> ApiKeys:
    keys: ApiKeys | None = getattr(request.app.state, "api_keys", None)
    if keys is None:
        raise HTTPException(503, "API 密钥服务还没有准备好")
    return keys


async def _app_exists(request: Request, app: str) -> None:
    if await _service(request).kind(app) is None:
        raise HTTPException(404, f"没有应用 {app}")


# Keys (builders)


@router.get("/apps/{app}/keys")
async def list_keys(app: str, request: Request) -> list[ApiKeyOut]:
    await _app_exists(request, app)
    return [ApiKeyOut(**k.out()) for k in await _keys(request).store.list(app)]


@router.post("/apps/{app}/keys", status_code=status.HTTP_201_CREATED)
async def create_key(app: str, body: CreateKey, request: Request) -> NewApiKey:
    await _app_exists(request, app)
    try:
        key, secret = await _keys(request).create(app, body.name, who(request, None))
    except TooManyKeys as exc:
        raise HTTPException(409, "一个应用最多 20 个密钥") from exc
    return NewApiKey(**key.out(), secret=secret)


@router.delete("/apps/{app}/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(app: str, key_id: str, request: Request) -> Response:
    if not await _keys(request).store.delete(app, key_id):
        raise HTTPException(404, "没有找到这个密钥")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# The app API: only keys, never the sign-in cookie.


async def _key(request: Request) -> ApiKey:
    key = await _keys(request).check(bearer(request.headers.get("authorization")))
    if key is None:
        raise HTTPException(401, "缺少或无效的 API 密钥", headers={"WWW-Authenticate": "Bearer"})
    request.state.api_key = key
    return key


def _result(request: Request, view: Any) -> RunResult:
    return RunResult(**run_result(view, request.app.state.web_url))


async def _own_run(request: Request, key: ApiKey, run_id: str) -> Any:
    try:
        view = await _service(request).get(run_id)
    except RunNotFound as exc:
        raise HTTPException(404, "没有找到这次运行") from exc
    if view.record.app != key.app:
        raise HTTPException(404, "没有找到这次运行")
    return view


def _invalid(where: str, exc: InvalidValues) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": ["body", where, k], "msg": message, "type": "value_error"}
            for k, message in exc.problems.items()
        ]
    )


@router.post("/v1/apps/{app}/runs", status_code=status.HTTP_201_CREATED)
async def start_run(app: str, body: StartRun, request: Request) -> RunResult:
    key = await _key(request)
    if key.app != app:
        raise HTTPException(403, f"这个密钥不能调用应用 {app}")
    service = _service(request)
    try:
        record = await service.create(
            app,
            body.input,
            requester=f"API：{key.name}",
            project=body.project.model_dump() if body.project else None,
        )
    except (UnknownApp, UnknownVersion) as exc:
        raise HTTPException(404, f"没有应用 {app}") from exc
    except InvalidValues as exc:
        raise _invalid("input", exc) from exc
    except (ModelNotAllowed, ProjectProblem) as exc:
        raise HTTPException(422, str(exc)) from exc
    return _result(request, await settle(service, record.run_id, body.wait))


@router.get("/v1/runs/{run_id}")
async def get_run(
    run_id: str, request: Request, wait: Annotated[int, Query(ge=0, le=60)] = 0
) -> RunResult:
    key = await _key(request)
    await _own_run(request, key, run_id)
    return _result(request, await settle(_service(request), run_id, wait))


@router.post("/v1/runs/{run_id}/answer")
async def answer(run_id: str, body: Answer, request: Request) -> RunResult:
    key = await _key(request)
    view = await _own_run(request, key, run_id)
    pending = view.pending
    if view.record.status != "waiting" or pending is None:
        raise HTTPException(409, "这次运行现在没有在等表单")
    if pending.get("kind") != "form":
        raise HTTPException(403, "这一步要审批者在平台上确认")
    options = pending.get("options") or []
    service = _service(request)
    try:
        await service.resume(
            run_id, options[0] if options else "submit", None, f"API：{key.name}", body.values
        )
    except NotWaiting as exc:
        raise HTTPException(409, "这次运行现在没有在等表单") from exc
    except InvalidValues as exc:
        raise _invalid("values", exc) from exc
    return _result(request, await settle(service, run_id, body.wait))
