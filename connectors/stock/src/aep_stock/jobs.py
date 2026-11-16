"""Long jobs (docs/api/connector-jobs.md) for downloads: hand back a job, report
progress, keep the result. Jobs live in memory only: after a restart a job is unknown,
and the node's retry downloads again (a file already downloaded is used at once)."""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aep_stock.pixabay import StockError

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
REUSED = ("queued", "running", "done")

Report = Callable[[float, str], None]
Work = Callable[[Report], Awaitable[Any]]


@dataclass
class Job:
    job_id: str
    key: str | None
    status: str
    created: float = field(default_factory=time.time)
    progress: float | None = None
    message: str | None = None
    result: Any = None
    error: str | None = None
    task: asyncio.Task[None] | None = None


class Jobs:
    def __init__(self, *, slots: int, keep_hours: float) -> None:
        self.slots = asyncio.Semaphore(slots)
        self.keep_seconds = keep_hours * 3600
        self.jobs: dict[str, Job] = {}
        self.keys: dict[str, str] = {}

    def envelope(self, job: Job) -> dict[str, Any]:
        return {
            "aep_job": {
                "job_id": job.job_id,
                "status": job.status,
                "progress": None if job.progress is None else round(job.progress, 4),
                "message": job.message,
                "result": job.result if job.status == "done" else None,
                "error": job.error if job.status == "error" else None,
            }
        }

    def find(self, key: str | None) -> Job | None:
        """The job this key already started, unless it failed or was cancelled."""
        if not key:
            return None
        job = self.jobs.get(self.keys.get(key, ""))
        return job if job is not None and job.status in REUSED else None

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise StockError(f"没有这个任务：{job_id}")
        return job

    def start(self, key: str | None, work: Work) -> Job:
        self._forget()
        job = Job(job_id=f"j_{secrets.token_hex(8)}", key=key, status="queued")
        self.jobs[job.job_id] = job
        if key:
            self.keys[key] = job.job_id
        job.task = asyncio.create_task(self._run(job, work))
        return job

    def done(self, key: str | None, result: Any) -> Job:
        """A job that had nothing left to do: the file was already there."""
        self._forget()
        job = Job(
            job_id=f"j_{secrets.token_hex(8)}", key=key, status="done", progress=1.0, result=result
        )
        self.jobs[job.job_id] = job
        if key:
            self.keys[key] = job.job_id
        return job

    async def _run(self, job: Job, work: Work) -> None:
        def report(progress: float, message: str) -> None:
            job.progress = max(job.progress or 0.0, min(1.0, progress))
            job.message = message

        try:
            async with self.slots:
                job.status = "running"
                job.result = await work(report)
            job.status, job.progress, job.message = "done", 1.0, None
        except asyncio.CancelledError:
            job.status, job.message = "cancelled", None
        except StockError as exc:
            job.status, job.error, job.message = "error", str(exc), None
        except Exception as exc:  # noqa: BLE001 - reported as the job's failure
            log.exception("job %s failed", job.job_id)
            job.status, job.error, job.message = "error", f"出错了：{type(exc).__name__}", None

    async def wait(self, job: Job, seconds: float) -> None:
        """Give a quick job a moment to finish before answering."""
        if job.task is not None and not job.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(job.task), seconds)
            except TimeoutError:
                pass

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in ACTIVE and job.task is not None:
            job.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(job.task), 10)
            except (TimeoutError, asyncio.CancelledError):
                pass
        return job

    async def close(self) -> None:
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _forget(self) -> None:
        """Finished jobs older than the keep time are dropped."""
        cutoff = time.time() - self.keep_seconds
        for job in [j for j in self.jobs.values() if j.status not in ACTIVE]:
            if job.created < cutoff:
                del self.jobs[job.job_id]
                if job.key and self.keys.get(job.key) == job.job_id:
                    del self.keys[job.key]
