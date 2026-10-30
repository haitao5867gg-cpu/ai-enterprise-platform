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
- ``status``: ``running``, ``waiting`` (for a person), ``done``, ``error`` or
  ``cancelled``.
- ``created_at``, ``updated_at`` (the last status change) and ``finished_at`` (when
  it ended: ``done``, ``error`` or ``cancelled``; null before), ISO times.
- ``step``: the node the run is at (``node`` id and ``title``), the one it waits at,
  or the last one it reached; null before its first node starts. While the run is
  ``waiting`` this is the human node it waits at, so there is no separate field for it.
- ``outcome``: what a finished flow run came to: the ``outcome`` of the end node it
  ended at (``delivered``, ``stopped`` or another word the app chose; see
  docs/api/flow-apps.md, "end"). Null unless the run is ``done`` at an end node that
  has one, so null for code apps and for runs from before it existed. The run's
  ``status`` stays ``done`` either way.
- ``waiting_since``: when the current wait for a person began (null unless
  waiting), and ``wait_seconds``: the seconds spent in waits already over (for a
  decision, or for someone to retry a failure). The
  time the machine worked is the run's whole time less both. Null for runs from
  before waits were counted.
- ``cancelled_by``: who cancelled it (a cancelled run's ``error`` is the reason
  given, or null); ``rerun_of``: the run it was copied from, or null.

``GET /runs/{id}`` also has the run's ``input``; ``inputs``: what a copy of the run
asks for, the start node's inputs (as in docs/api/flow-apps.md) of the configuration
``rerun`` uses: the run's own, or the current version's for a run from before
versions (null for a code app); ``failed`` (for a failed run):
``node`` and ``title`` of the step it failed at (null when unknown),
``error_type`` (as a flow node's ``error_type`` in docs/api/flow-apps.md, or
``max_visits``: a node ran as often as it may, ``max_steps``: the run took too many
steps; null when unknown) and ``retryable`` (the failure may be temporary: a
timeout, a model, tool, knowledge base, Databricks or sandbox error, or unknown;
``input_error`` and the others are not);
and ``actions``: which of ``cancel``, ``retry`` and ``rerun`` it allows now.

``messages`` is how the run went, oldest first. Each has ``role`` (``user``: a person;
``assistant``: a step of the app), ``content`` (an assistant's is Markdown; a person's is
shown as written), ``node`` and ``node_title`` (the step that said it), ``actor`` (who
said it: the requester for a flow app's start message, whoever decided for a human
step; null when not given, and for assistant messages) and ``at`` (when it was said, ISO
time). The four are null in messages from before they existed and in the ``etl`` app's
messages. docs/api/flow-apps.md ("运行") lists which steps of a flow app say what.
``sources`` are the knowledge snippets a flow app's run numbered (``[]`` for other runs),
each with ``cited``: whether what the run shows refers to it by ``[n]``
(docs/api/knowledge.md, "运行记录里的出处").

``POST /runs/{id}/cancel`` (``actor``, ``reason``: optional) cancels a running or
waiting run (202; 409 when it is in another status). A run executing in another
process stops before its next node; work already done outside the platform stays.
``POST /runs/{id}/retry`` (``actor``: optional) continues a failed run from the
step that failed (202; 409 when it is not failed, for instance because someone
retried it already, or when its failure is not ``failed.retryable``; the
``detail`` says which).
``POST /runs/{id}/rerun`` (``input``, ``requester``: optional) starts a new run of
the same app version with the run's input, or ``input`` in its place (send all of
it, not only what changed) (201, like ``POST /runs``);
a draft's run copies the configuration it ran with. 404 when the app or its version
is gone. These three accept an empty body, or none.

A waiting run's ``pending`` is an approval: show ``prompt`` and offer
``options`` (shown as ``option_labels`` when given); any option may carry a
comment, and those in ``comment_required`` must (the resume is refused with 422
without one). A flow app's human node may ask for a ``form`` instead: fill in
``fields`` and resume with ``decision`` ``submit`` and ``values``. A flow app's
approval may also carry ``fields``: questions, every one of which must be answered
in ``values`` (each always ``required``, with no ``default``, and with ``suggested``,
``why`` and ``affects`` when given); and ``changed_branch``: the option a decision
turns into when an answer differs from its ``suggested``, or a comment or a note is
given (null when the node sets none, and in runs from before it existed). Options whose
``requires`` did not hold when the node paused are left out of ``options``,
``option_labels`` and ``comment_required``. Apps can attach ``details``
for a richer view, tagged by ``details.type``:

- ``flow_human`` (flow apps): ``node`` and ``show``; a node that sets ``view`` or
  ``data``, or an approval that sets ``fields`` (however many questions it gets when
  it pauses; a setting that is null counts as not set, and a form's ``fields`` are the
  form itself, not a setting of this kind), also has ``view``, ``data`` (its values,
  references replaced), ``round`` (1 the first time this node waits in the run, 2 the
  second, and so on), ``seen`` (``sha256:`` and the hex digest of what is shown) and
  ``dropped_fields`` (questions that did not pass the checks when the node paused).

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
optional ``actor`` (who decided, up to 100 characters; until sign-in exists the
client provides it). For a flow app's human node it also takes ``values`` (a form's
answers, or the answers to an approval's questions), ``notes`` (up to 50
``{item, text}``: a remark on one thing the page shows, where ``item`` is
``kind:id`` and the id must be found in ``details.data``; approvals only) and
``seen`` (the ``details.seen`` the page showed). In this order: 409 when the run is
not waiting; 409 ``内容已经更新，请刷新后再处理`` when ``seen`` is given and the
pending has a ``details.seen`` that differs (nothing is compared when either is
missing); 422 ``这一步现在不能选「…」`` when ``decision`` is not in ``options``; 422
with ``loc`` ``["body", "values", key]`` or ``["body", "notes", i, "item" | "text"]``
for a bad answer or note, or ``["body", "notes"]`` for more than 50 notes or
non-empty notes on a form (null and ``[]`` count as none). A decision not in ``comment_required``
then turns into ``changed_branch`` when an answer differs from its suggestion, or a
comment or a note is given; a decision in ``comment_required`` needs a comment, a
note or a changed answer (422 ``这个决定需要写明意见``). docs/api/flow-apps.md
("人工节点在工作台上的样子") has the full rules and the node's outputs. The ``etl``
app's approvals have no questions and no data to note on.

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
``GET /runs/{id}/files.zip`` downloads a run's code as a zip, from the first of these
that holds an object of path → text: ``result.files`` (any app, the ``etl`` app's
included); ``result.code.files`` (a flow end node output ``code`` holding an
``etl_code_check`` bundle); otherwise, whatever the run's status (done, waiting,
failed or cancelled), the ``bundle`` output of the last ``etl_code_check`` step (in
``steps`` order, as ``GET /runs/{id}/flow`` lists them) that has one. A step's
``output`` is the state update its node wrote, so that bundle is
``output["vars"][<node id>]["bundle"]``; a step is an ``etl_code_check`` step when
its node's ``type`` in ``nodes`` is ``etl_code_check``. Values of
another shape are skipped, never a 500. The zip is named after the bundle
(``result.bundle`` when it is text, as for ``etl``, else the bundle's ``name``), or
the run id; 404 ``这次运行没有可下载的代码`` when there is none.

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

from aep.knowledge.citations import CitedSource
from aep.runtime.service import (
    CommentRequired,
    ContentChanged,
    InvalidDecision,
    InvalidNotes,
    InvalidValues,
    NoDraftToRun,
    NotCancellable,
    NotFailed,
    NotRetryable,
    NotWaiting,
    RunNotFound,
    RunService,
    RunView,
    UnknownApp,
    UnknownVersion,
    retryable,
)
from aep.runtime.store import FINISHED, RunRecord, RunStatus

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


class Note(BaseModel):
    # ``kind:id``: one thing the page shows, found in ``pending.details.data``.
    item: str
    text: str


class Resume(BaseModel):
    decision: str
    comment: str | None = None
    # Who decided. Until sign-in exists (M4) the client says so; it goes into the
    # run's approval record.
    actor: str | None = Field(default=None, max_length=100)
    # A form's answers, or the answers to an approval's questions, by field key.
    values: dict[str, Any] | None = None
    # Remarks on single things the page shows (approvals only); their number and
    # length are checked after the decision and the values, in the documented order.
    notes: list[Note] | None = None
    # The ``details.seen`` of the page the decision was made on.
    seen: str | None = None


class Cancel(BaseModel):
    actor: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=500)


class Retry(BaseModel):
    actor: str | None = Field(default=None, max_length=100)


class Rerun(BaseModel):
    input: dict[str, Any] | None = None
    requester: str | None = Field(default=None, max_length=100)


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
    cancelled_by: str | None
    rerun_of: str | None
    outcome: str | None


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    # Null in messages from before they existed, and in the etl app's.
    node: str | None = None
    node_title: str | None = None
    actor: str | None = None
    at: str | None = None


class Pending(BaseModel):
    kind: str
    prompt: str
    options: list[str]
    comment_required: list[str] = []
    option_labels: dict[str, str] | None = None
    fields: list[dict[str, Any]] | None = None
    # The option a decision turns into when an answer or a remark changes it.
    changed_branch: str | None = None
    details: dict[str, Any] | None = None


class Failed(BaseModel):
    node: str | None
    title: str | None
    error_type: str | None
    retryable: bool


class Actions(BaseModel):
    cancel: bool
    retry: bool
    rerun: bool


class RunDetail(RunSummary):
    input: dict[str, Any]
    inputs: list[dict[str, Any]] | None
    failed: Failed | None
    actions: Actions
    messages: list[Message]
    pending: Pending | None
    result: dict[str, Any] | None
    error: str | None
    # Langfuse trace of this run; null when tracing is off.
    trace_id: str | None = None
    sources: list[CitedSource] = []


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
        "finished_at": r.updated_at.isoformat() if r.status in FINISHED else None,
        "step": r.step,
        "waiting_since": r.waiting_since.isoformat() if r.waiting_since else None,
        "wait_seconds": r.wait_seconds,
        "cancelled_by": r.cancelled_by,
        "rerun_of": r.rerun_of,
        "outcome": r.outcome if r.status == "done" else None,
    }


async def _detail(v: RunView, service: RunService, app_titles: dict[str, str]) -> RunDetail:
    r = v.record
    step = r.step or {}
    return RunDetail(
        **_summary(r, service, app_titles),
        input=r.input,
        inputs=await service.copy_inputs(r),
        failed=Failed(
            node=step.get("node"),
            title=step.get("title"),
            error_type=r.error_type,
            retryable=retryable(r),
        )
        if r.status == "error"
        else None,
        actions=Actions(
            cancel=r.status in ("running", "waiting"),
            retry=retryable(r),
            rerun=await service.can_rerun(r),
        ),
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
    return await _detail(view, service, await service.app_titles())


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
        record = await service.resume(
            run_id,
            body.decision,
            body.comment,
            body.actor,
            body.values,
            notes=[n.model_dump() for n in body.notes] if body.notes is not None else None,
            seen=body.seen,
        )
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    except NotWaiting as exc:
        raise HTTPException(409, "run is not waiting for a decision") from exc
    except ContentChanged as exc:
        raise HTTPException(409, "内容已经更新，请刷新后再处理") from exc
    except InvalidDecision as exc:
        raise HTTPException(422, f"这一步现在不能选「{body.decision}」") from exc
    except CommentRequired as exc:
        raise HTTPException(422, "这个决定需要写明意见") from exc
    except InvalidValues as exc:
        raise _invalid_values("values", exc) from exc
    except InvalidNotes as exc:
        raise RequestValidationError(
            [
                {"loc": ["body", "notes", *where], "msg": message, "type": "value_error"}
                for where, message in exc.problems
            ]
        ) from exc
    return RunRef(run_id=record.run_id, status=record.status)


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(run_id: str, service: Service, body: Cancel | None = None) -> RunRef:
    body = body or Cancel()
    try:
        record = await service.cancel(
            run_id, (body.actor or "").strip() or None, (body.reason or "").strip() or None
        )
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    except NotCancellable as exc:
        raise HTTPException(409, "只有运行中或等待确认的运行可以取消") from exc
    return RunRef(run_id=record.run_id, status=record.status)


@router.post("/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_run(run_id: str, service: Service, body: Retry | None = None) -> RunRef:
    try:
        record = await service.retry(run_id)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    except NotFailed as exc:
        raise HTTPException(409, "这次运行已经不在失败状态，可能有人已经重试过") from exc
    except NotRetryable as exc:
        raise HTTPException(409, "这次运行不能重试，请复制后重新发起") from exc
    return RunRef(run_id=record.run_id, status=record.status)


@router.post("/{run_id}/rerun", status_code=status.HTTP_201_CREATED)
async def rerun_run(run_id: str, service: Service, body: Rerun | None = None) -> RunRef:
    body = body or Rerun()
    try:
        record = await service.rerun(
            run_id, body.input, requester=(body.requester or "").strip() or None
        )
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    except (UnknownApp, UnknownVersion) as exc:
        raise HTTPException(404, "这次运行用的应用或版本已经不在了") from exc
    except InvalidValues as exc:
        raise _invalid_values("input", exc) from exc
    return RunRef(run_id=record.run_id, status=record.status)


def _files(value: Any) -> dict[str, str] | None:
    """``value`` when it is a non-empty object of path → text."""
    if not isinstance(value, dict) or not value:
        return None
    if all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return value
    return None


def _named(bundle: Any) -> str | None:
    name = bundle.get("name") if isinstance(bundle, dict) else None
    return name if isinstance(name, str) and name else None


async def _run_files(run_id: str, service: RunService) -> tuple[dict[str, str], str | None] | None:
    """The code a run has to download and the bundle's name: ``result.files``, then
    ``result.code.files``, then the bundle of the run's last code check step."""
    view = await service.get(run_id)
    result = view.result if isinstance(view.result, dict) else {}
    files = _files(result.get("files"))
    if files is not None:
        bundle = result.get("bundle")
        return files, bundle if isinstance(bundle, str) and bundle else None
    code = result.get("code")
    files = _files(code.get("files")) if isinstance(code, dict) else None
    if files is not None:
        return files, _named(code)
    flow = await service.flow(run_id)
    checks = {n["id"] for n in flow["nodes"] if n.get("type") == "etl_code_check"}
    for step in reversed(flow["steps"]):
        if step["node"] not in checks or not isinstance(step["output"], dict):
            continue
        written = step["output"].get("vars")
        mine = written.get(step["node"]) if isinstance(written, dict) else None
        bundle = mine.get("bundle") if isinstance(mine, dict) else None
        files = _files(bundle.get("files")) if isinstance(bundle, dict) else None
        if files is not None:
            return files, _named(bundle)
    return None


@router.get("/{run_id}/files.zip")
async def download_files(run_id: str, service: Service) -> Response:
    """The code a run produced (an ETL bundle), as a zip archive."""
    try:
        found = await _run_files(run_id, service)
    except RunNotFound as exc:
        raise HTTPException(404, "run not found") from exc
    if found is None:
        raise HTTPException(404, "这次运行没有可下载的代码")
    files, bundle = found
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", bundle or run_id)
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )
