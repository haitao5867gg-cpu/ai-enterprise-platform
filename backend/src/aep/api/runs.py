"""Runs API. The frontend (issue #4) is built against this contract.

``POST /runs`` takes the ``app``, its ``input`` and optionally ``app_version``
(an app version number, see ``GET /apps/{app}/versions``; the current one when
left out) or ``draft`` (run the app's draft), and ``requester``: who asks for
the run (up to 100 characters; until sign-in exists the client says so). A flow
app's input must fit its start node's inputs (422 with ``loc``
``["body", "input", key]``); see docs/api/flow-apps.md for flow apps.

``GET /runs`` lists the latest runs (``limit``, at most 200), newest first. Every
run record (in the list and in ``GET /runs/{id}``) has:

- ``app_title``: the app's title; ``app_version``: the version the run uses for its
  whole life (null for the runs of a draft, and for runs from before versions);
  ``draft``: whether it runs a draft (its eval, or someone trying it).
- ``title``: what the run is about, in one line of at most 80 characters: a flow
  app's first text input that was filled in, in the order of its start node's
  inputs; a code app's ``request``. Null when there is none.
- ``created_by``: the ``requester`` it was started with, or null.
- ``created_at``, ``updated_at`` (the last status change) and ``finished_at`` (when
  it ended, ``done`` or ``error``; null before), ISO times.
- ``step``: the node the run is at (``node`` id and ``title``), the one it waits at,
  or the last one it reached; null before its first node starts.
- ``waiting_since``: when the current wait for a person began (null unless
  waiting), and ``wait_seconds``: the seconds spent in waits already over. The
  time the machine worked is the run's whole time less both. Null for runs from
  before waits were counted.

A waiting run's ``pending`` is an approval: show ``prompt`` and offer
``options`` (shown as ``option_labels`` when given); any option may carry a
comment, and those in ``comment_required`` must (the resume is refused with 422
without one). A flow app's human node may ask for a ``form`` instead: fill in
``fields`` and resume with ``decision`` ``submit`` and ``values``. Apps can attach ``details``
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
``skills``; for a flow app only ``title`` and ``summary`` are filled, the other three
are empty), or null. ``nodes`` are the graph's nodes in order, ``__start__`` and
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
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from aep.knowledge.citations import Source
from aep.runtime.service import (
    CommentRequired,
    InvalidDecision,
    InvalidValues,
    NoDraftToRun,
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
    # Run the app's draft as it is now (trying it before publishing).
    draft: bool = False
    # Who asks for the run. Until sign-in exists (M4) the client says so.
    requester: str | None = Field(default=None, max_length=100)


class Resume(BaseModel):
    decision: str
    comment: str | None = None
    # Who decided. Until sign-in exists (M4) the client says so; it goes into the
    # run's approval record.
    actor: str | None = None
    # A form's answers (``pending.kind`` is ``form``), by field key.
    values: dict[str, Any] | None = None


class RunRef(BaseModel):
    run_id: str
    status: RunStatus


class Step(BaseModel):
    node: str
    title: str


class RunSummary(BaseModel):
    run_id: str
    app: str
    app_title: str
    app_version: int | None
    draft: bool
    status: RunStatus
    title: str | None
    created_by: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    step: Step | None
    waiting_since: str | None
    wait_seconds: float | None


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class Pending(BaseModel):
    kind: str
    prompt: str
    options: list[str]
    comment_required: list[str] = []
    option_labels: dict[str, str] | None = None
    fields: list[dict[str, Any]] | None = None
    details: dict[str, Any] | None = None


class RunDetail(RunSummary):
    messages: list[Message]
    pending: Pending | None
    result: dict[str, Any] | None
    error: str | None
    # Langfuse trace of this run; null when tracing is off.
    trace_id: str | None = None
    sources: list[Source] = []


class FlowNode(BaseModel):
    id: str
    title: str
    kind: str
    type: str | None = None
    model: str | None = None
    skills: list[dict[str, Any]] | None = None
    design: dict[str, Any] | None = None


class FlowEdge(BaseModel):
    source: str
    target: str
    conditional: bool
    label: str | None = None


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


def _is_draft(r: RunRecord) -> bool:
    # A draft's runs keep its configuration but belong to no version; runs from before
    # versions have neither.
    return r.app_version is None and r.config is not None


def _summary(r: RunRecord, service: RunService, app_titles: dict[str, str]) -> dict[str, Any]:
    return {
        "run_id": r.run_id,
        "app": r.app,
        "app_title": app_titles.get(r.app, r.app),
        "app_version": r.app_version,
        "draft": _is_draft(r),
        "status": r.status,
        "title": service.title(r),
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
        "finished_at": r.updated_at.isoformat() if r.status in ("done", "error") else None,
        "step": r.step,
        "waiting_since": r.waiting_since.isoformat() if r.waiting_since else None,
        "wait_seconds": r.wait_seconds,
    }


def _detail(v: RunView, service: RunService, app_titles: dict[str, str]) -> RunDetail:
    return RunDetail(
        **_summary(v.record, service, app_titles),
        messages=[Message(**m) for m in v.messages],
        pending=Pending(**v.pending) if v.pending else None,
        result=v.result,
        error=v.record.error,
        trace_id=v.trace_id,
        sources=v.sources,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(body: CreateRun, service: Service) -> RunRef:
    if body.draft and body.app_version is not None:
        raise HTTPException(422, "draft 和 app_version 不能同时给")
    try:
        record = await service.create(
            body.app,
            body.input,
            body.app_version,
            draft=body.draft,
            requester=(body.requester or "").strip() or None,
        )
    except UnknownApp as exc:
        raise HTTPException(404, f"没有应用 {body.app}") from exc
    except UnknownVersion as exc:
        raise HTTPException(404, f"应用 {body.app} 没有版本 {body.app_version}") from exc
    except NoDraftToRun as exc:
        raise HTTPException(409, f"应用 {body.app} 没有草稿") from exc
    except InvalidValues as exc:
        raise _invalid_values("input", exc) from exc
    return RunRef(run_id=record.run_id, status=record.status)


def _invalid_values(where: str, exc: InvalidValues) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": ["body", where, key], "msg": message, "type": "value_error"}
            for key, message in exc.problems.items()
        ]
    )


@router.get("")
async def list_runs(service: Service, limit: int = 50) -> list[RunSummary]:
    titles = await service.app_titles()
    runs = await service.recent(min(max(limit, 1), 200))
    return [RunSummary(**_summary(r, service, titles)) for r in runs]


@router.get("/{run_id}")
async def get_run(run_id: str, service: Service) -> RunDetail:
    try:
        view = await service.get(run_id)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    return _detail(view, service, await service.app_titles())


@router.get("/{run_id}/flow")
async def get_flow(run_id: str, service: Service) -> Flow:
    try:
        return Flow(**await service.flow(run_id))
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc


@router.get("/{run_id}/manual")
async def get_manual(run_id: str, request: Request) -> dict[str, Any]:
    """The manual of the app configuration this run used (docs/api/data-nodes.md)."""
    try:
        return await request.app.state.catalog.run_manual(run_id)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc


@router.post("/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_run(run_id: str, body: Resume, service: Service) -> RunRef:
    try:
        record = await service.resume(run_id, body.decision, body.comment, body.actor, body.values)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    except NotWaiting as exc:
        raise HTTPException(409, "run is not waiting for a decision") from exc
    except InvalidDecision as exc:
        raise HTTPException(422, f"invalid decision: {body.decision}") from exc
    except CommentRequired as exc:
        raise HTTPException(422, "这个决定需要写明意见") from exc
    except InvalidValues as exc:
        raise _invalid_values("values", exc) from exc
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
