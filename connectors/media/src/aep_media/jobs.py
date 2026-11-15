"""Long jobs (docs/api/connector-jobs.md): hand back a job, report progress, keep results.

Finished jobs are written under ``_work/.jobs`` so a restarted connector still knows
them; a job that was still going when the process stopped is recorded as failed, and a
retry under the same key starts a new one (its finished shots come from the cache).
"""

import asyncio
import json
import logging
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aep_media.paths import CACHE, JOBS, Media, MediaError

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
REUSED = ("queued", "running", "done")
RESTARTED = "连接器重启了，任务没做完"
SWEEP_SECONDS = 3600

Report = Callable[[float, str], None]
Work = Callable[[Path, Report], Awaitable[Any]]


@dataclass
class Job:
    job_id: str
    tool: str
    key: str | None
    status: str
    created: float = field(default_factory=time.time)
    progress: float | None = None
    message: str | None = None
    result: Any = None
    error: str | None = None
    queued: bool = False
    slot: bool = False
    turn: asyncio.Event | None = None
    task: asyncio.Task[None] | None = None

    def record(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "tool": self.tool,
            "key": self.key,
            "status": self.status,
            "created": self.created,
            "progress": self.progress,
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }


class Jobs:
    def __init__(self, media: Media, *, slots: int, keep_hours: float) -> None:
        self.media = media
        self.keep_seconds = keep_hours * 3600
        self.slots = slots
        self.busy = 0
        self.jobs: dict[str, Job] = {}
        self.keys: dict[str, str] = {}
        self.waiting: list[str] = []
        self.swept = 0.0
        self._load()

    # Records ---------------------------------------------------------------------------

    @property
    def records(self) -> Path:
        return self.media.work / JOBS

    def _save(self, job: Job) -> None:
        self.records.mkdir(parents=True, exist_ok=True)
        path = self.records / f"{job.job_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job.record(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _load(self) -> None:
        if not self.records.is_dir():
            return
        for path in sorted(self.records.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(
                    job_id=str(data["job_id"]),
                    tool=str(data["tool"]),
                    key=data.get("key"),
                    status=str(data["status"]),
                    created=float(data["created"]),
                    progress=data.get("progress"),
                    message=data.get("message"),
                    result=data.get("result"),
                    error=data.get("error"),
                )
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("skipping unreadable job record %s", path.name)
                continue
            if job.status in ACTIVE:
                job.status, job.error, job.message = "error", RESTARTED, None
                self._save(job)
            self.jobs[job.job_id] = job
            # A retried key has several records: it stands for the newest one (job ids
            # are random, so the file order says nothing).
            newest = self.jobs.get(self.keys.get(job.key or "", ""))
            if job.key and (newest is None or job.created >= newest.created):
                self.keys[job.key] = job.job_id

    # Asking ----------------------------------------------------------------------------

    def envelope(self, job: Job) -> dict[str, Any]:
        message = job.message
        if job.status == "queued":
            ahead = self.busy + (
                self.waiting.index(job.job_id) if job.job_id in self.waiting else 0
            )
            message = f"排队中，前面还有 {ahead} 个"
        return {
            "aep_job": {
                "job_id": job.job_id,
                "status": job.status,
                "progress": None if job.progress is None else round(job.progress, 4),
                "message": message,
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
            raise MediaError(f"没有这个任务：{job_id}")
        return job

    # Running ---------------------------------------------------------------------------

    def start(self, tool: str, key: str | None, work: Work, *, queued: bool) -> Job:
        """Begin ``work`` in its own directory; queued jobs wait for a free slot."""
        self.sweep()
        job = Job(
            job_id=f"j_{secrets.token_hex(8)}",
            tool=tool,
            key=key,
            status="running",
            queued=queued,
        )
        self.jobs[job.job_id] = job
        if key:
            self.keys[key] = job.job_id
        if queued:
            if self.busy < self.slots:
                self.busy += 1
                job.slot = True
            else:
                job.status, job.turn = "queued", asyncio.Event()
                self.waiting.append(job.job_id)
        self._save(job)
        job.task = asyncio.create_task(self._run(job, work))
        return job

    async def _run(self, job: Job, work: Work) -> None:
        directory = self.media.work / job.job_id

        def report(progress: float, message: str) -> None:
            job.progress = max(job.progress or 0.0, min(1.0, progress))
            job.message = message

        try:
            if job.turn is not None:
                await job.turn.wait()
                job.status = "running"
            directory.mkdir(parents=True, exist_ok=True)
            job.result = await work(directory, report)
            job.status, job.progress, job.message = "done", 1.0, None
        except asyncio.CancelledError:
            job.status, job.message = "cancelled", None
        except MediaError as exc:
            job.status, job.error, job.message = "error", str(exc), None
        except Exception as exc:  # noqa: BLE001 - reported as the job's failure
            log.exception("job %s failed", job.job_id)
            job.status, job.error, job.message = "error", f"出错了：{exc}", None
        finally:
            if job.job_id in self.waiting:
                self.waiting.remove(job.job_id)
            if job.slot:
                job.slot = False
                self._next()
            if job.status != "done":
                shutil.rmtree(directory, ignore_errors=True)
            self._save(job)

    def _next(self) -> None:
        """A slot is free: the longest-waiting job takes it."""
        self.busy -= 1
        while self.waiting:
            nxt = self.jobs[self.waiting.pop(0)]
            if nxt.status == "queued" and nxt.turn is not None:
                self.busy += 1
                nxt.slot = True
                nxt.turn.set()
                return

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
        elif job.status in ACTIVE:
            job.status = "cancelled"
            self._save(job)
        return job

    async def close(self) -> None:
        for job in self.jobs.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()
        tasks = [job.task for job in self.jobs.values() if job.task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)

    # Cleaning --------------------------------------------------------------------------

    def sweep(self, now: float | None = None) -> None:
        """Remove work files, cached shots and job records older than the keep time."""
        now = now or time.time()
        if now - self.swept < SWEEP_SECONDS:
            return
        self.swept = now
        cutoff = now - self.keep_seconds
        active = {job.job_id for job in self.jobs.values() if job.status in ACTIVE}
        work = self.media.work
        if not work.is_dir():
            return
        for path in work.iterdir():
            try:
                if path.name in (JOBS, CACHE) or path.name in active:
                    continue
                if path.lstat().st_mtime < cutoff:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
            except OSError:
                continue
        for folder in (work / CACHE, work / JOBS):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                try:
                    if path.stem in active or path.lstat().st_mtime >= cutoff:
                        continue
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                job = self.jobs.pop(path.stem, None)
                if job is not None and job.key and self.keys.get(job.key) == job.job_id:
                    del self.keys[job.key]
