"""Runs API. The frontend (issue #4) is built against this contract."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from aep.runtime.service import (
    InvalidDecision,
    NotWaiting,
    RunNotFound,
    RunService,
    RunView,
    UnknownApp,
)
from aep.runtime.store import RunRecord, RunStatus

router = APIRouter(prefix="/runs", tags=["runs"])


def get_run_service(request: Request) -> RunService:
    return request.app.state.run_service


Service = Annotated[RunService, Depends(get_run_service)]


class CreateRun(BaseModel):
    app: str
    input: dict[str, Any] = {}


class Resume(BaseModel):
    decision: str
    comment: str | None = None


class RunRef(BaseModel):
    run_id: str
    status: RunStatus


class RunSummary(BaseModel):
    run_id: str
    app: str
    status: RunStatus
    created_at: str
    updated_at: str


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class Pending(BaseModel):
    kind: str
    prompt: str
    options: list[str]


class RunDetail(BaseModel):
    run_id: str
    app: str
    status: RunStatus
    messages: list[Message]
    pending: Pending | None
    result: dict[str, Any] | None
    error: str | None


def _summary(r: RunRecord) -> RunSummary:
    return RunSummary(
        run_id=r.run_id,
        app=r.app,
        status=r.status,
        created_at=r.created_at.isoformat(),
        updated_at=r.updated_at.isoformat(),
    )


def _detail(v: RunView) -> RunDetail:
    return RunDetail(
        run_id=v.record.run_id,
        app=v.record.app,
        status=v.record.status,
        messages=[Message(**m) for m in v.messages],
        pending=Pending(**v.pending) if v.pending else None,
        result=v.result,
        error=v.record.error,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(body: CreateRun, service: Service) -> RunRef:
    try:
        record = await service.create(body.app, body.input)
    except UnknownApp as exc:
        raise HTTPException(404, f"unknown app: {body.app}; available: {service.apps}") from exc
    return RunRef(run_id=record.run_id, status=record.status)


@router.get("")
async def list_runs(service: Service, limit: int = 50) -> list[RunSummary]:
    return [_summary(r) for r in await service.recent(min(max(limit, 1), 200))]


@router.get("/{run_id}")
async def get_run(run_id: str, service: Service) -> RunDetail:
    try:
        return _detail(await service.get(run_id))
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc


@router.post("/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_run(run_id: str, body: Resume, service: Service) -> RunRef:
    try:
        record = await service.resume(run_id, body.decision, body.comment)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    except NotWaiting as exc:
        raise HTTPException(409, "run is not waiting for a decision") from exc
    except InvalidDecision as exc:
        raise HTTPException(422, f"invalid decision: {body.decision}") from exc
    return RunRef(run_id=record.run_id, status=record.status)
