"""``frames``: stills from a video, for a vision model to look at."""

import asyncio
from pathlib import Path
from typing import Any

from aep_media.ffmpeg import FFmpeg
from aep_media.paths import Media, MediaError
from aep_media.probe import inspect, snapshot


def _seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise MediaError(f"{name}要是不小于 0 的秒数")
    return float(value)


def times(
    duration: float,
    at: list[Any] | None,
    every_seconds: float | None,
    count: int,
    max_frames: int,
) -> list[float]:
    """Where to take frames, in order, inside the video and at most ``max_frames``."""
    if at is not None:
        if not isinstance(at, list):
            raise MediaError("at 要是一组秒数")
        found = sorted({round(_seconds(t, "at 里的时间"), 3) for t in at})
        found = [t for t in found if t < duration]
    elif every_seconds is not None:
        step = _seconds(every_seconds, "every_seconds")
        if step <= 0:
            raise MediaError("every_seconds 要大于 0")
        found = []
        t = 0.0
        while t < duration and len(found) < max_frames:
            found.append(round(t, 3))
            t += step
    else:
        found = [round((i + 0.5) * duration / count, 3) for i in range(count)]
    return found[:max_frames]


async def frames(
    media: Media,
    ff: FFmpeg,
    source: Any,
    *,
    at: list[Any] | None,
    every_seconds: float | None,
    count: int,
    max_frames: int,
    width: int,
    task: str | None = None,
) -> dict[str, Any]:
    if not 1 <= count <= 100:
        raise MediaError("count 要在 1–100 之间")
    if not 1 <= max_frames <= 100:
        raise MediaError("max_frames 要在 1–100 之间")
    if not 64 <= width <= 1920:
        raise MediaError("width 要在 64–1920 之间")
    path = media.resolve(source)
    found = await inspect(ff, path)
    if found["kind"] == "audio":
        raise MediaError("音频没有画面，不能抽帧")
    out_dir = media.task_dir(task)
    if found["kind"] == "image":
        points = [0.0]
    else:
        points = times(found["duration_seconds"] or 0, at, every_seconds, count, max_frames)
    limit = asyncio.Semaphore(4)

    async def one(index: int, at_time: float) -> dict[str, Any]:
        out: Path = out_dir / f"frame_{index + 1:03d}.jpg"
        async with limit:
            got_width, got_height = await snapshot(ff, path, at_time, out, width=width)
        return {
            "time": at_time,
            "width": got_width,
            "height": got_height,
            "image": media.file_ref(out),
        }

    taken = await asyncio.gather(*(one(i, t) for i, t in enumerate(points)))
    return {"duration_seconds": found["duration_seconds"], "frames": list(taken)}
