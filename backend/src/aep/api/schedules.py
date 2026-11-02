"""Schedules API: runs an app starts on a schedule (docs/api/schedules.md)."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from aep.api.runs import ProjectChoice, RunRef, Service, _invalid_values
from aep.auth import who
from aep.config import Settings, get_settings
from aep.runtime.service import (
    InvalidValues,
    ModelNotAllowed,
    ProjectProblem,
    RunService,
    UnknownApp,
    UnknownVersion,
)
from aep.runtime.store import RunRecord
from aep.schedules import (
    MAX_PER_APP,
    Cron,
    CronError,
    Schedule,
    Scheduler,
    ScheduleStore,
    Start,
    new_id,
    zone,
)

router = APIRouter(tags=["schedules"])


def starter(service: RunService) -> Start:
    """How a schedule starts its run: like POST /runs, the app's current version, as the
    schedule's creator, marked with the schedule (docs/api/schedules.md, 到点时发生什么)."""

    async def start(s: Schedule) -> RunRecord:
        return await service.create(
            s.app,
            s.input,
            requester=s.created_by,
            project=s.project,
            trigger={"kind": "schedule", "schedule_id": s.schedule_id, "title": s.title},
        )

    return start


def get_store(request: Request) -> ScheduleStore:
    return request.app.state.schedules


def get_scheduler(request: Request) -> Scheduler:
    return request.app.state.scheduler


Store = Annotated[ScheduleStore, Depends(get_store)]
Runner = Annotated[Scheduler, Depends(get_scheduler)]


class ScheduleBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    cron: str = Field(min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    input: dict[str, Any] = {}
    project: ProjectChoice | None = None
    enabled: bool = True
    # Who sets it up. Until sign-in is on the client says so (as for runs).
    requester: str | None = Field(default=None, max_length=100)


class SchedulePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    input: dict[str, Any] | None = None
    project: ProjectChoice | None = None
    enabled: bool | None = None


class LastRun(BaseModel):
    run_id: str
    at: datetime
    status: str | None


class ScheduleOut(BaseModel):
    schedule_id: str
    app: str
    title: str
    cron: str
    timezone: str
    input: dict[str, Any]
    project: dict[str, Any] | None
    enabled: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None
    next_runs: list[datetime]
    last_run: LastRun | None
    last_error: str | None


async def _out(s: Schedule, service: Service) -> ScheduleOut:
    upcoming: list[datetime] = []
    if s.enabled:
        upcoming = Cron.parse(s.cron).upcoming(datetime.now(UTC), zone(s.timezone))
    last = None
    if s.last_run_id and s.last_run_at:
        last = LastRun(
            run_id=s.last_run_id, at=s.last_run_at, status=await service.status_of(s.last_run_id)
        )
    return ScheduleOut(
        schedule_id=s.schedule_id,
        app=s.app,
        title=s.title,
        cron=s.cron,
        timezone=s.timezone,
        input=s.input,
        project=s.project,
        enabled=s.enabled,
        created_by=s.created_by,
        created_at=s.created_at,
        updated_at=s.updated_at,
        next_run_at=s.next_run_at,
        next_runs=upcoming,
        last_run=last,
        last_error=s.last_error,
    )


async def _check(service: Service, s: Schedule) -> None:
    """Whether the schedule could start a run now (docs/api/schedules.md, 保存时检查)."""
    try:
        Cron.parse(s.cron)
        zone(s.timezone)
    except CronError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        await service.check_start(s.app, s.input, s.project)
    except UnknownApp as exc:
        raise HTTPException(404, f"没有应用 {s.app}") from exc
    except UnknownVersion as exc:
        raise HTTPException(409, f"应用 {s.app} 还没有发布，不能定时运行") from exc
    except InvalidValues as exc:
        raise _invalid_values("input", exc) from exc
    except (ModelNotAllowed, ProjectProblem) as exc:
        raise HTTPException(422, str(exc)) from exc


async def _found(store: ScheduleStore, schedule_id: str) -> Schedule:
    found = await store.get(schedule_id)
    if found is None:
        raise HTTPException(404, f"没有定时 {schedule_id}")
    return found


@router.get("/apps/{app}/schedules")
async def list_schedules(app: str, store: Store, service: Service) -> list[ScheduleOut]:
    if await service.kind(app) is None:
        raise HTTPException(404, f"没有应用 {app}")
    return [await _out(s, service) for s in await store.for_app(app)]


@router.post("/apps/{app}/schedules", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    app: str,
    body: ScheduleBody,
    store: Store,
    service: Service,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ScheduleOut:
    now = datetime.now(UTC)
    schedule = Schedule(
        schedule_id=new_id(),
        app=app,
        title=body.title.strip() or body.title,
        cron=" ".join(body.cron.split()),
        timezone=body.timezone or settings.schedule_timezone,
        input=body.input,
        project=body.project.model_dump() if body.project else None,
        enabled=body.enabled,
        created_by=who(request, body.requester),
        created_at=now,
        updated_at=now,
    )
    await _check(service, schedule)
    if len(await store.for_app(app)) >= MAX_PER_APP:
        raise HTTPException(409, f"每个应用最多 {MAX_PER_APP} 个定时")
    schedule.next_run_at = schedule.due_after(now)
    await store.put(schedule)
    return await _out(schedule, service)


class Preview(BaseModel):
    cron: str
    timezone: str
    next_runs: list[datetime]


@router.get("/schedules/preview")
async def preview(
    cron: str, settings: Annotated[Settings, Depends(get_settings)], timezone: str | None = None
) -> Preview:
    """When a schedule with this cron and time zone would run next, before saving it."""
    text = " ".join(cron.split())
    where = timezone or settings.schedule_timezone
    try:
        upcoming = Cron.parse(text).upcoming(datetime.now(UTC), zone(where))
    except CronError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Preview(cron=text, timezone=where, next_runs=upcoming)


@router.get("/schedules/{schedule_id}")
async def get_schedule(schedule_id: str, store: Store, service: Service) -> ScheduleOut:
    return await _out(await _found(store, schedule_id), service)


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str, body: SchedulePatch, store: Store, service: Service
) -> ScheduleOut:
    found = await _found(store, schedule_id)
    given = body.model_dump(exclude_unset=True)
    changed = replace(found, updated_at=datetime.now(UTC))
    for key in ("title", "timezone", "input", "enabled"):
        if given.get(key) is not None:
            setattr(changed, key, given[key])
    if body.cron is not None:
        changed.cron = " ".join(body.cron.split())
    if "project" in given:
        changed.project = body.project.model_dump() if body.project else None
    await _check(service, changed)
    changed.next_run_at = changed.due_after(changed.updated_at)
    await store.put(changed)
    return await _out(changed, service)


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: str, store: Store) -> Response:
    if not await store.delete(schedule_id):
        raise HTTPException(404, f"没有定时 {schedule_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/schedules/{schedule_id}/run", status_code=status.HTTP_201_CREATED)
async def run_schedule(schedule_id: str, store: Store, runner: Runner) -> RunRef:
    found = await _found(store, schedule_id)
    try:
        record = await runner.start_now(found)
    except (UnknownApp, UnknownVersion) as exc:
        raise HTTPException(409, f"应用 {found.app} 不能运行了：{exc}") from exc
    except InvalidValues as exc:
        raise _invalid_values("input", exc) from exc
    except (ModelNotAllowed, ProjectProblem) as exc:
        raise HTTPException(422, str(exc)) from exc
    return RunRef(run_id=record.run_id, status=record.status)
