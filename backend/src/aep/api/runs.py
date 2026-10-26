"""Runs API. The frontend (issue #4) is built against this contract.

A waiting run's ``pending`` is always an approval: show ``prompt`` and offer
``options``; ``reject`` may carry a comment. Apps can attach ``details`` for a
richer view, tagged by ``details.type``:

- ``etl_plan`` (app ``etl``): ``details.plan`` is the proposed ETL plan, as
  ``aep.etl.plan.EtlPlan``: ``summary``, ``language``, and ``targets`` with
  ``name``, ``description``, ``sources``, ``logic``, ``columns`` (``name``,
  ``source``, ``description``), ``primary_key``, ``load`` (``mode``, ``key``) and
  ``quality_rules`` (``name``, ``condition``, ``on_violation``).

A finished ``etl`` run's ``result`` has ``bundle``, ``language``, ``plan``,
``files`` (path → content), ``checks`` (``passed``, ``problems``: the static
checks) and ``verification``, the run on the Databricks dev target:
``status`` is ``skipped`` (with ``reason``), ``passed`` or ``failed``; a run that
happened also has ``mode`` (``pipeline``: the bundle was deployed and run;
``cluster``: only its transformations ran, on ``cluster_id``), ``stage``
(validate, deploy or run), ``attempt``, ``errors``, ``warnings``, ``url`` (the
pipeline, or the cluster, in the workspace) and ``tables`` (``name``, ``status``,
``rows``, ``dropped``, ``expectations`` with ``name``, ``passed`` and ``failed``
row counts). ``GET /runs/{id}/files.zip`` downloads the files.
"""

import io
import re
import zipfile
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
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
    details: dict[str, Any] | None = None


class RunDetail(BaseModel):
    run_id: str
    app: str
    status: RunStatus
    messages: list[Message]
    pending: Pending | None
    result: dict[str, Any] | None
    error: str | None
    # Langfuse trace of this run; null when tracing is off.
    trace_id: str | None = None


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
        trace_id=v.trace_id,
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


@router.get("/{run_id}/files.zip")
async def download_files(run_id: str, service: Service) -> Response:
    """The files a finished run produced (e.g. an ETL bundle), as a zip archive."""
    try:
        view = await service.get(run_id)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    files = (view.result or {}).get("files")
    if not files:
        raise HTTPException(404, "this run has no files")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    name = str((view.result or {}).get("bundle") or run_id)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )
