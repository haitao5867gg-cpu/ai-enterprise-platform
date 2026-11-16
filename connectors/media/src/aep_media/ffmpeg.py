"""Running ffprobe and ffmpeg: errors in the caller's words, progress, and stopping."""

import asyncio
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from aep_media.config import Settings
from aep_media.paths import MediaError

Progress = Callable[[float], None]


def _tail(text: bytes, limit: int = 600) -> str:
    lines = [line.strip() for line in text.decode(errors="replace").splitlines() if line.strip()]
    tail = " / ".join(lines[-3:])
    return tail[-limit:] if tail else "没有输出"


class FFmpeg:
    def __init__(self, settings: Settings) -> None:
        self.ffmpeg = settings.ffmpeg
        self.ffprobe = settings.ffprobe
        self.threads = settings.media_ffmpeg_threads

    async def _spawn(
        self, *args: str, stdout: int | None, cwd: Path | None = None
    ) -> asyncio.subprocess.Process:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"}
        for key in ("HOME", "FONTCONFIG_FILE", "FONTCONFIG_PATH", "XDG_CACHE_HOME"):
            if key in os.environ:
                env[key] = os.environ[key]
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                pass

    async def probe(self, path: Path, what: str) -> dict[str, Any]:
        """ffprobe's streams and format; ``what`` names the kind in the error."""
        process = await self._spawn(
            self.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 25)
        except TimeoutError:
            await self._stop(process)
            raise MediaError(f"不是有效的{what}：探测超过 25 秒") from None
        except asyncio.CancelledError:
            await self._stop(process)
            raise
        if process.returncode != 0:
            raise MediaError(f"不是有效的{what}：{_tail(err)}")
        try:
            data = json.loads(out or b"{}")
        except ValueError:
            raise MediaError(f"不是有效的{what}：探测结果读不出来") from None
        return data if isinstance(data, dict) else {}

    async def run(
        self,
        args: Sequence[str],
        *,
        seconds: float | None = None,
        progress: Progress | None = None,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> None:
        """Run ffmpeg; ``progress`` gets the done share of ``seconds`` of output."""
        command = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if progress is not None:
            command += ["-progress", "pipe:1", "-nostats"]
        command += list(args)
        process = await self._spawn(
            *command,
            stdout=asyncio.subprocess.PIPE if progress else asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )

        async def read_progress() -> None:
            assert process.stdout is not None
            async for raw in process.stdout:
                key, _, value = raw.decode(errors="replace").strip().partition("=")
                if key == "out_time_us" and seconds and progress:
                    try:
                        done = int(value) / 1_000_000
                    except ValueError:
                        continue
                    progress(max(0.0, min(1.0, done / seconds)))

        async def finish() -> bytes:
            assert process.stderr is not None
            reader = asyncio.create_task(read_progress()) if progress else None
            err = await process.stderr.read()
            await process.wait()
            if reader is not None:
                await reader
            return err

        try:
            err = await asyncio.wait_for(finish(), timeout)
        except TimeoutError:
            await self._stop(process)
            raise MediaError("ffmpeg 处理超时") from None
        except asyncio.CancelledError:
            await self._stop(process)
            raise
        if process.returncode != 0:
            raise MediaError(f"ffmpeg 出错：{_tail(err)}")

    def encode_threads(self) -> list[str]:
        return ["-threads", str(self.threads)] if self.threads else []
