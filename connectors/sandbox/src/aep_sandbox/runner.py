"""Runs one piece of code in a fresh child process (see ``aep_sandbox.child``).

The child gets an empty environment and an empty work directory, and its limits:
CPU time, address space and file size. The runner adds a wall clock limit, caps
what it reads back, and kills the child's whole process group when time is up.
"""

import asyncio
import json
import os
import signal
import sys
import tempfile
from typing import Any

from aep_sandbox.config import Settings

# Starting Python and importing pandas is not the user's time.
GRACE_SECONDS = 2


async def _read(stream: asyncio.StreamReader, limit: int, tail: bool = False) -> tuple[bytes, bool]:
    """Everything the stream gives, but at most ``limit`` bytes kept (the first, or the
    last with ``tail``), and whether some were dropped."""
    kept, dropped = b"", False
    while chunk := await stream.read(65536):
        kept += chunk
        if len(kept) > limit:
            dropped = True
            kept = kept[-limit:] if tail else kept[:limit]
    return kept, dropped


class Runner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._slots = asyncio.Semaphore(settings.sandbox_parallel)

    async def run(self, code: str, inputs: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        s = self._settings
        timeout = max(1, min(timeout_seconds, s.sandbox_max_timeout_seconds))
        request = json.dumps({"code": code, "inputs": inputs}, ensure_ascii=False).encode()
        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="run-", dir=s.sandbox_work_dir) as work:
                return await self._run(request, timeout, work)

    async def _run(self, request: bytes, timeout: int, work: str) -> dict[str, Any]:
        s = self._settings
        env = {
            "HOME": work,
            "TMPDIR": work,
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        limits = [str(timeout), str(s.sandbox_memory_mb << 20), str(s.sandbox_max_file_bytes)]
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "aep_sandbox.child",
            *limits,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work,
            env=env,
            start_new_session=True,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        out = asyncio.ensure_future(_read(proc.stdout, s.sandbox_max_output_bytes))
        err = asyncio.ensure_future(_read(proc.stderr, s.sandbox_max_log_chars * 4, tail=True))
        timed_out = False
        try:
            proc.stdin.write(request)
            await proc.stdin.drain()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout + GRACE_SECONDS)
        except TimeoutError:
            timed_out = True
        except (BrokenPipeError, ConnectionResetError):
            await proc.wait()
        finally:
            if proc.returncode is None or timed_out:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        (output, too_much), (logs_raw, _) = await asyncio.gather(out, err)
        logs = logs_raw.decode("utf-8", "replace")[-s.sandbox_max_log_chars :]
        if timed_out or proc.returncode == -signal.SIGXCPU:
            return {"status": "timeout", "error": f"运行超过 {timeout} 秒", "logs": logs}
        if too_much:
            mb = s.sandbox_max_output_bytes / (1 << 20)
            return {"status": "error", "error": f"返回值超过 {mb:g} MB", "logs": logs}
        try:
            reply = json.loads(output)
        except ValueError:
            how = f"信号 {-proc.returncode}" if proc.returncode < 0 else f"退出码 {proc.returncode}"
            return {"status": "error", "error": f"代码进程异常结束（{how}）", "logs": logs}
        if "error" in reply:
            return {"status": "error", "error": reply["error"], "logs": logs}
        return {"status": "ok", "result": reply["result"], "logs": logs}
