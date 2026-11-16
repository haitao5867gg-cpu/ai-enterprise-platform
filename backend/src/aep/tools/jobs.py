"""Following a connector's long-running jobs (docs/api/connector-jobs.md).

A connector tool that takes minutes hands back ``{"aep_job": {...}}`` at once instead of
its result; :func:`follow` asks the same connector's ``aep_job_status`` until the job is
done, reporting its progress, and cancels it with ``aep_job_cancel`` when it runs too
long, the run is cancelled or the work it was for is given up (:func:`abandonable`). The
calls themselves go through ``aep.tools.mcp.McpTools``, so each one keeps the ordinary tool
timeout.
"""

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NoReturn

import anyio

from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError

JOB = "aep_job"
JOB_KEY = "aep/job_key"
STATUS_TOOL = "aep_job_status"
CANCEL_TOOL = "aep_job_cancel"
STATUSES = ("queued", "running", "done", "error", "cancelled")
MAX_JOB_ID = 200
MAX_MESSAGE = 200
MAX_ERROR = 2000
# How often the status is asked: every FAST_SECONDS for the first FAST_FOR seconds, then
# every SLOW_SECONDS; a connector that cannot be reached for UNREACHABLE_SECONDS in a row
# fails the job, and a cancel waits at most CANCEL_SECONDS. Tests make them shorter.
FAST_SECONDS = 2.0
FAST_FOR = 30.0
SLOW_SECONDS = 5.0
UNREACHABLE_SECONDS = 120.0
CANCEL_SECONDS = 5.0


class JobFailed(ToolError):
    """The job ended in error, the connector cancelled it, or its status could not be read;
    the message is what to show after 工具报错：."""


class JobTimedOut(Exception):
    """The job ran past ``AEP_TOOL_JOB_SECONDS`` and was cancelled."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"长任务超过 {seconds:g} 秒还没做完，已经取消")


class JobStopped(Exception):
    """The run was cancelled (seen from another process) while the job ran; the job is
    cancelled too. The caller stops the run, not just the node."""


@dataclass(frozen=True)
class Job:
    job_id: str
    status: str
    progress: float | None
    message: str | None
    result: Any
    error: str | None


@dataclass
class Watch:
    """What following a job reports to and asks of the run: ``progress`` gets the job's
    progress (0–1 or None) and message whenever either changes; ``stopped`` says whether
    the run was cancelled meanwhile."""

    progress: Callable[[float | None, str | None], Awaitable[None]] | None = None
    stopped: Callable[[], Awaitable[bool]] | None = None


class Abandon:
    """Work that may be given up while its run goes on, such as the items of an each node
    still running after one failed. A job followed in a task started under it is
    cancelled when that task is cancelled after :meth:`give_up`, as for a cancelled run;
    otherwise a cancelled task leaves its job for the next start, as when the process
    stops."""

    def __init__(self) -> None:
        self.given_up = False

    def give_up(self) -> None:
        self.given_up = True


_ABANDON: ContextVar[tuple[Abandon, ...]] = ContextVar("aep_jobs_abandon", default=())


@contextmanager
def abandonable() -> Iterator[Abandon]:
    """An :class:`Abandon` for the tasks started within: each takes a copy of the context,
    so it follows its jobs under this one (and any it is nested in)."""
    abandon = Abandon()
    token = _ABANDON.set((*_ABANDON.get(), abandon))
    try:
        yield abandon
    finally:
        _ABANDON.reset(token)


def _given_up() -> bool:
    return any(a.given_up for a in _ABANDON.get())


def job_key(run_id: str, node: str, visit: int, tool: str, args: Any) -> str:
    """The same for the same call in the same execution of a node, so a node executed
    again after a restart picks its job up (docs/api/connector-jobs.md, 任务键)."""
    text = json.dumps(
        [run_id, node, visit, tool, args], ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(text.encode()).hexdigest()


def is_job(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {JOB}


def parse(value: Any) -> Job:
    """The job in a tool's result; :class:`JobFailed` when it is not a proper one."""
    job = value.get(JOB) if isinstance(value, dict) else None
    if not isinstance(job, dict):
        raise JobFailed("工具返回的任务格式不对：aep_job 要是一个对象")
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not 1 <= len(job_id) <= MAX_JOB_ID:
        raise JobFailed(f"工具返回的任务格式不对：job_id 要是 1–{MAX_JOB_ID} 个字符")
    status = job.get("status")
    if status not in STATUSES:
        raise JobFailed(f"工具返回的任务格式不对：不认识状态 {status!r}")
    progress = job.get("progress")
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        progress = None
    else:
        progress = min(max(float(progress), 0.0), 1.0)
    message = job.get("message")
    message = message[:MAX_MESSAGE] if isinstance(message, str) and message.strip() else None
    error = job.get("error")
    error = str(error)[:MAX_ERROR] if error not in (None, "") else None
    return Job(job_id, status, progress, message, job.get("result"), error)


async def follow(
    tools: McpTools,
    url: str,
    first: Any,
    watch: Watch,
    *,
    call_seconds: float | None,
    limit_seconds: float,
) -> Any:
    """The result of the job ``first`` describes, once it is done."""
    job = parse(first)
    started = time.monotonic()
    unreachable_since: float | None = None
    shown: tuple[float | None, str | None] = (None, None)
    try:
        while True:
            if watch.progress is not None and (job.progress, job.message) != shown:
                shown = (job.progress, job.message)
                await watch.progress(job.progress, job.message)
            if job.status == "done":
                return job.result
            if job.status == "error":
                raise JobFailed(job.error or "任务失败了，连接器没有说原因")
            if job.status == "cancelled":
                raise JobFailed("任务被连接器取消了")
            elapsed = time.monotonic() - started
            if elapsed >= limit_seconds:
                await _time_out(tools, url, job.job_id, limit_seconds)
            pause = FAST_SECONDS if elapsed < FAST_FOR else SLOW_SECONDS
            await anyio.sleep(min(pause, limit_seconds - elapsed))
            # A status call ends with the job's time: an answer after it does not count.
            left = limit_seconds - (time.monotonic() - started)
            if left <= 0:
                await _time_out(tools, url, job.job_id, limit_seconds)
            seconds = min(call_seconds or tools.timeout_seconds, left)
            reply: Any = None
            failure: Exception | None = None
            try:
                reply = await tools.call(url, STATUS_TOOL, {"job_id": job.job_id}, seconds)
            except ConnectorUnavailable as exc:
                now = time.monotonic()
                unreachable_since = unreachable_since or now
                if now - unreachable_since >= UNREACHABLE_SECONDS:
                    failure = exc
            except ToolError as exc:
                failure = JobFailed(f"查任务进度失败：{_said(exc)}")
                failure.__cause__ = exc
            # Asked after the call, whatever it came to: a cancel wins over its answer
            # (even done) and over its error, so a cancelled run never meets error handling.
            if watch.stopped is not None and await _stopped(watch.stopped):
                await _cancel(tools, url, job.job_id)
                raise JobStopped()
            if failure is not None:
                raise failure
            if reply is None:
                continue
            unreachable_since = None
            job = parse(reply)
    except anyio.get_cancelled_exc_class():
        # The run was cancelled in this process, the work was given up, or the process is
        # stopping. Only the last leaves the job: after a restart the node runs again and
        # picks its job up by its key. Without a run to ask, nothing will pick it up.
        with anyio.CancelScope(shield=True):
            if _given_up() or watch.stopped is None or await _stopped(watch.stopped):
                await _cancel(tools, url, job.job_id)
        raise


async def _stopped(stopped: Callable[[], Awaitable[bool]]) -> bool:
    """Whether the run was cancelled; False when that cannot be told in time."""
    with anyio.move_on_after(CANCEL_SECONDS):
        try:
            return await stopped()
        except Exception:  # noqa: BLE001 - an unanswered question is no cancel
            return False
    return False


async def _time_out(tools: McpTools, url: str, job_id: str, limit_seconds: float) -> NoReturn:
    await _cancel(tools, url, job_id)
    raise JobTimedOut(limit_seconds)


async def _cancel(tools: McpTools, url: str, job_id: str) -> None:
    """Ask the connector to stop the job; it may not offer to, and a failure is ignored."""
    with anyio.move_on_after(CANCEL_SECONDS):
        try:
            await tools.call(url, CANCEL_TOOL, {"job_id": job_id}, CANCEL_SECONDS)
        except Exception:  # noqa: BLE001 - cancelling is a courtesy
            pass


def _said(exc: ToolError) -> str:
    text = str(exc)
    for prefix in (f"{STATUS_TOOL}: ", f"Error executing tool {STATUS_TOOL}: "):
        text = text.removeprefix(prefix)
    return text
