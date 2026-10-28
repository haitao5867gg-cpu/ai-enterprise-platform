"""Runs API. The frontend (issue #4) is built against this contract.

``POST /runs`` takes the ``app``, its ``input`` and optionally ``app_version``
(an app version number, see ``GET /apps/{app}/versions``; the current one when
left out). Every run record has ``app_version``: the version it uses for its whole
life (null for the runs of a draft's eval, and for runs from before versions).

A waiting run's ``pending`` is always an approval: show ``prompt`` and offer
``options``; any option may carry a comment, and those in ``comment_required``
must (the resume is refused with 422 without one). Apps can attach ``details``
for a richer view, tagged by ``details.type``:

- ``etl_plan`` (app ``etl``): ``details.plan`` is the proposed ETL plan, as
  ``aep.etl.plan.EtlPlan``: ``summary``, ``language``, and ``targets`` with
  ``name``, ``description``, ``sources``, ``logic``, ``columns`` (``name``,
  ``source``, ``description``), ``primary_key``, ``load`` (``mode``, ``key``) and
  ``quality_rules`` (``name``, ``condition``, ``on_violation``), and
  ``assumptions`` (strings the user should check; may be empty).
- ``etl_release`` (app ``etl``): the bundle passed on the Databricks dev target
  and waits for approval to go to prod. ``details`` has ``bundle``,
  ``verification`` (the dev run, as in the result below) and ``target``:
  ``mode`` (``job`` or ``pipeline``), ``catalog``, ``schema``, ``schedule``
  (Quartz cron), ``timezone`` and ``cluster_id`` (job mode). ``reject`` sends
  the code back to be rewritten with the comment.

``POST /runs/{id}/resume`` takes ``decision``, an optional ``comment`` and an
optional ``actor`` (who decided; until sign-in exists the client provides it).

A finished ``etl`` run's ``result`` has ``bundle``, ``language``, ``plan``,
``files`` (path → content), ``checks`` (``passed``, ``problems``: the static
checks) and ``verification``, the run on the Databricks dev target:
``status`` is ``skipped`` (with ``reason``), ``passed`` or ``failed``; a run that
happened also has ``mode`` (``pipeline``: the bundle was deployed and run;
``job``: deployed as a notebook job on ``cluster_id`` and run, with ``job_id``
and ``run_id``; ``cluster``: only its transformations ran, on ``cluster_id``),
``stage``
(validate, deploy or run), ``attempt``, ``errors``, ``warnings``, ``url`` (the
pipeline, or the cluster, in the workspace) and ``tables`` (``name``, ``status``,
``rows``, ``dropped``, ``expectations`` with ``name``, ``passed`` and ``failed``
row counts). ``approvals`` lists every decision so far: ``step`` (``plan`` or
``release``), ``decision``, ``comment``, ``actor`` and ``at`` (ISO time).
``release`` is the prod deploy: ``status`` ``skipped`` (with ``reason``: no prod
target configured, or the dev run did not pass), ``deployed`` or ``failed``,
with ``mode``, ``catalog``, ``schema``, ``schedule``, ``timezone``, ``job_id``,
``pipeline_id``, ``cluster_id``, ``url``, ``steps`` and ``errors``; it is absent
while the run waits for the release approval.
``asset`` is the bundle's PR in the assets repository (after a passing dev run):
``status`` ``skipped`` (with ``reason``), ``failed`` (with ``errors``), or
``proposed`` (the PR is open), then ``released``, ``rejected`` or
``release_failed`` once the release decision is noted on it; with ``repo``,
``path``, ``version``, ``pr_number``, ``pr_url``, ``branch`` and ``commit``, and
``errors`` if noting a decision on the PR failed. ``GET /assets`` lists them.
``GET /runs/{id}/files.zip`` downloads the files.

``GET /runs/{id}/flow`` shows how the run's app is built and what this run did,
for explaining it (see ``aep.runtime.design``). ``design`` is the app as a whole
(``title``, ``summary``, ``pattern``: a fixed workflow or an agent, ``knowledge``,
``skills``), or null. ``nodes`` are the graph's nodes in order, ``__start__`` and
``__end__`` included: ``id``, ``title``, ``kind`` (``tool``, ``model``, ``human``,
``code``, or ``terminal`` for start and end), ``model`` (the alias this run uses,
for model nodes), ``skills`` (``name`` and ``version`` of each skill the node
uses, for nodes with a configuration) and ``design`` (``summary``, ``inputs``,
``outputs``, ``tools``, ``model_step``, ``prompt``: the system prompt this run
used, ``knowledge``, ``checks``, ``retry``; null if the node has none). ``edges``
have ``source``, ``target`` and ``conditional`` (taken only
when its condition holds). ``steps`` is every node this run executed, oldest first:
``id``, ``node``, ``status`` (``done``, ``running``, ``waiting`` for a person,
``error``, or ``stopped``), ``started_at``, ``input`` (the state the node saw,
without the conversation), ``output`` (what it wrote; null until it finishes),
``error`` and ``interrupt`` (what it asked a person, if it did). ``current`` is
the node the run is at, or null once it is done.
"""

import io
import re
import zipfile
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from aep.runtime.service import (
    CommentRequired,
    InvalidDecision,
    NotWaiting,
    RunNotFound,
    RunService,
    RunView,
    UnknownApp,
    UnknownVersion,
)
from aep.runtime.store import RunRecord, RunStatus

router = APIRouter(prefix="/runs", tags=["runs"])


def get_run_service(request: Request) -> RunService:
    return request.app.state.run_service


Service = Annotated[RunService, Depends(get_run_service)]


class CreateRun(BaseModel):
    app: str
    input: dict[str, Any] = {}
    app_version: int | None = None


class Resume(BaseModel):
    decision: str
    comment: str | None = None
    # Who decided. Until sign-in exists (M4) the client says so; it goes into the
    # run's approval record.
    actor: str | None = None


class RunRef(BaseModel):
    run_id: str
    status: RunStatus


class RunSummary(BaseModel):
    run_id: str
    app: str
    app_version: int | None
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
    comment_required: list[str] = []
    details: dict[str, Any] | None = None


class RunDetail(BaseModel):
    run_id: str
    app: str
    app_version: int | None
    status: RunStatus
    messages: list[Message]
    pending: Pending | None
    result: dict[str, Any] | None
    error: str | None
    # Langfuse trace of this run; null when tracing is off.
    trace_id: str | None = None


class FlowNode(BaseModel):
    id: str
    title: str
    kind: str
    model: str | None = None
    skills: list[dict[str, Any]] | None = None
    design: dict[str, Any] | None = None


class FlowEdge(BaseModel):
    source: str
    target: str
    conditional: bool


class FlowStep(BaseModel):
    id: str
    node: str
    status: Literal["done", "running", "waiting", "error", "stopped"]
    started_at: str | None
    input: dict[str, Any]
    output: Any
    error: str | None
    interrupt: Any


class Flow(BaseModel):
    design: dict[str, Any] | None
    nodes: list[FlowNode]
    edges: list[FlowEdge]
    steps: list[FlowStep]
    current: str | None


def _summary(r: RunRecord) -> RunSummary:
    return RunSummary(
        run_id=r.run_id,
        app=r.app,
        app_version=r.app_version,
        status=r.status,
        created_at=r.created_at.isoformat(),
        updated_at=r.updated_at.isoformat(),
    )


def _detail(v: RunView) -> RunDetail:
    return RunDetail(
        run_id=v.record.run_id,
        app=v.record.app,
        app_version=v.record.app_version,
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
        record = await service.create(body.app, body.input, body.app_version)
    except UnknownApp as exc:
        raise HTTPException(404, f"unknown app: {body.app}; available: {service.apps}") from exc
    except UnknownVersion as exc:
        raise HTTPException(404, f"应用 {body.app} 没有版本 {body.app_version}") from exc
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


@router.get("/{run_id}/flow")
async def get_flow(run_id: str, service: Service) -> Flow:
    try:
        return Flow(**await service.flow(run_id))
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc


@router.post("/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_run(run_id: str, body: Resume, service: Service) -> RunRef:
    try:
        record = await service.resume(run_id, body.decision, body.comment, body.actor)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    except NotWaiting as exc:
        raise HTTPException(409, "run is not waiting for a decision") from exc
    except InvalidDecision as exc:
        raise HTTPException(422, f"invalid decision: {body.decision}") from exc
    except CommentRequired as exc:
        raise HTTPException(422, "这个决定需要写明意见") from exc
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
