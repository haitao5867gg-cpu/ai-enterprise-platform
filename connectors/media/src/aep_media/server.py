"""MCP Streamable HTTP: ``probe``, ``frames``, ``render`` and the long-job tools."""

from typing import Annotated, Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from aep_media.config import Settings
from aep_media.ffmpeg import FFmpeg
from aep_media.frames import frames as take_frames
from aep_media.jobs import Jobs
from aep_media.paths import Media, MediaError
from aep_media.probe import probe as probe_file
from aep_media.render import read_spec, render_spec

# They read the media volume and write only their own work files.
SAFE = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
FRAMES_WAIT = 20.0


def _key(ctx: Context) -> str | None:
    meta = ctx.request_context.meta or {}
    key = meta.get("aep/job_key") if hasattr(meta, "get") else None  # type: ignore[union-attr]
    return key if isinstance(key, str) and key else None


def build(settings: Settings | None = None) -> tuple[MCPServer, Jobs]:
    settings = settings or Settings()
    media = Media(settings.media_dir)
    media.prepare()
    ff = FFmpeg(settings)
    jobs = Jobs(media, slots=settings.media_max_jobs, keep_hours=settings.media_work_hours)
    server = MCPServer(
        "aep-media",
        version="0.1.0",
        instructions=(
            "Probes, samples and renders files in the media libraries. Paths are relative "
            "to the media volume; a source may also be a media item summary with `path` or "
            'a file reference {"aep_file": {"path": ...}}. Files made are file references '
            "under _work/, kept for 24 hours unless the platform saves them into a library."
        ),
    )

    @server.tool(annotations=SAFE)
    async def probe(source: Any, thumbnail: bool = False) -> dict[str, Any]:
        """What a video, audio or image file is: kind, duration, width and height (as
        shown, rotation applied), orientation, fps, codecs and size. With
        ``thumbnail`` a JPG 480 px on the long edge from second 1 is made and returned
        as a file reference."""
        try:
            return await probe_file(media, ff, source, thumbnail)
        except MediaError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=SAFE)
    async def frames(
        source: Any,
        ctx: Context,
        at: list[float] | None = None,
        every_seconds: float | None = None,
        count: Annotated[int, Field(ge=1, le=100)] = 6,
        max_frames: Annotated[int, Field(ge=1, le=100)] = 30,
        width: Annotated[int, Field(ge=64, le=1920)] = 512,
    ) -> dict[str, Any]:
        """Stills from a video for a vision model: at the given seconds (``at``), every
        ``every_seconds`` from 0, or else ``count`` evenly spaced; at most ``max_frames``,
        ``width`` px wide. Answers {"duration_seconds", "frames": [{"time", "width",
        "height", "image": file reference}]}, as a long job when it takes over 20 s."""
        key = _key(ctx)
        known = jobs.find(key)
        if known is not None:
            await jobs.wait(known, FRAMES_WAIT)
            return jobs.envelope(known)
        try:
            path = media.resolve(source)
        except MediaError as exc:
            raise ToolError(str(exc)) from None

        async def work(directory: Any, _report: Any) -> dict[str, Any]:
            return await take_frames(
                media,
                ff,
                media.relative(path),
                at=at,
                every_seconds=every_seconds,
                count=count,
                max_frames=max_frames,
                width=width,
                task=directory.name,
            )

        job = jobs.start("frames", key, work, queued=False)
        await jobs.wait(job, FRAMES_WAIT)
        if job.status == "error":
            raise ToolError(job.error or "抽帧失败")
        return jobs.envelope(job)

    @server.tool(annotations=SAFE)
    async def render(
        clips: list[dict[str, Any]],
        ctx: Context,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        background: str = "#000000",
        audio: list[dict[str, Any]] | None = None,
        subtitles: dict[str, Any] | None = None,
        duration: float | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render shots laid end to end (hard cuts) into one H.264/AAC MP4, always as a
        long job. ``clips``: 1-300 of {"source", "start" (s into a video), "duration"
        (needed for images), "fit": "contain" (default, letterboxed on ``background``) |
        "cover" (fill, crop at "focus" [x, y]) | "blur" (fit over a blurred fill),
        "volume" (the clip's own sound, default 0)}. ``audio``: 0-10 tracks of
        {"source", "at" (s into the video), "start", "duration", "volume", "loop",
        "fade_in", "fade_out"}. ``subtitles``: {"cues": [{"start", "end", "text"}] in
        video time, "style": {"size" (px at 1080 wide, default 60), "color", "outline",
        "outline_color", "y" (centre, 0-1 of the height, default 0.82), "max_chars"
        (per line, default 16), "bold"}}, burned in. ``duration`` cuts the video or holds
        the last shot. ``output``: {"name": "成片.mp4", "quality": "draft" | "standard"
        | "high"}. Result: {"video": file reference, "duration_seconds", "width",
        "height", "size_bytes"}. Finished shots are cached, so re-rendering with one
        shot changed redoes only that shot."""
        key = _key(ctx)
        known = jobs.find(key)
        if known is not None:
            return jobs.envelope(known)
        args = {
            "clips": clips,
            "width": width,
            "height": height,
            "fps": fps,
            "background": background,
            "audio": audio,
            "subtitles": subtitles,
            "duration": duration,
            "output": output,
        }
        try:
            spec = await read_spec(media, ff, settings, args)
        except MediaError as exc:
            raise ToolError(str(exc)) from None

        async def work(directory: Any, report: Any) -> dict[str, Any]:
            return await render_spec(media, ff, spec, directory, report)

        # The same key may have been handed in again while its sources were checked.
        known = jobs.find(key)
        if known is not None:
            return jobs.envelope(known)
        return jobs.envelope(jobs.start("render", key, work, queued=True))

    @server.tool(annotations=SAFE)
    async def aep_job_status(job_id: str) -> dict[str, Any]:
        """Where a job handed back by frames or render is, and its result when done."""
        try:
            return jobs.envelope(jobs.get(job_id))
        except MediaError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=SAFE)
    async def aep_job_cancel(job_id: str) -> dict[str, Any]:
        """Stop a job; its unfinished files are removed."""
        try:
            return jobs.envelope(await jobs.cancel(job_id))
        except MediaError as exc:
            raise ToolError(str(exc)) from None

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return server, jobs


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings()
    server, _jobs = build(settings)
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.media_mcp_allowed_hosts,
            allowed_origins=settings.media_mcp_allowed_origins,
        ),
    )


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.media_mcp_host, port=settings.media_mcp_port)


if __name__ == "__main__":
    main()
