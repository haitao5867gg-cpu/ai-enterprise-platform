"""MCP Streamable HTTP: ``search_videos``, ``download_video`` and the long-job tools
(docs/api/stock.md)."""

import asyncio
import re
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
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

from aep_stock.config import Settings
from aep_stock.jobs import Jobs
from aep_stock.pixabay import SITE, Clip, Pixabay, StockError

# They read a stock site and write only their own work files.
LOOKS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
WORK = "_work"
FOLDER = "stock"
THUMBS = "thumbs"
SWEEP_SECONDS = 3600
ID = re.compile(r"pixabay:(\d{1,12})")


def _key(ctx: Context) -> str | None:
    meta = ctx.request_context.meta or {}
    key = meta.get("aep/job_key") if hasattr(meta, "get") else None  # type: ignore[union-attr]
    return key if isinstance(key, str) and key else None


class Folder:
    """``_work/stock`` on the media volume: downloads, and thumbnails under ``thumbs``."""

    def __init__(self, media_dir: Path, keep_hours: float) -> None:
        self.root = media_dir.resolve()
        self.path = self.root / WORK / FOLDER
        self.keep_seconds = keep_hours * 3600
        self.swept = 0.0

    def prepare(self) -> None:
        for folder in (self.root / WORK, self.path, self.path / THUMBS):
            if folder.is_symlink():
                raise RuntimeError(f"{folder} 不能是软链接")
            folder.mkdir(exist_ok=True)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def ref(self, path: Path, name: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
        found: dict[str, Any] = {"path": self.relative(path), "name": name}
        if source is not None:
            found["source"] = source
        return {"aep_file": found}

    def fresh(self, path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return path.is_file() and time.time() - info.st_mtime < self.keep_seconds

    def sweep(self) -> None:
        """Remove downloads and thumbnails older than the keep time (at most hourly)."""
        now = time.time()
        if now - self.swept < SWEEP_SECONDS:
            return
        self.swept = now
        for folder in (self.path, self.path / THUMBS):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                try:
                    if path.is_file() and now - path.lstat().st_mtime >= self.keep_seconds:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue


def _found(clip: Clip, min_short_side: int, thumbnail: Any) -> dict[str, Any]:
    version = clip.version(min_short_side)
    assert version is not None
    return {
        "id": clip.id,
        "site": SITE,
        "url": clip.page,
        "author": clip.author or None,
        "tags": list(clip.tags),
        "seconds": clip.seconds,
        "width": version.width,
        "height": version.height,
        "orientation": version.orientation,
        "thumbnail": thumbnail,
    }


def build(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[MCPServer, Jobs, Pixabay]:
    settings = settings or Settings()
    folder = Folder(settings.media_dir, settings.stock_cache_hours)
    folder.prepare()
    pixabay = Pixabay(settings, transport)
    jobs = Jobs(slots=settings.stock_max_downloads, keep_hours=settings.stock_cache_hours)
    max_bytes = settings.stock_max_mb * 1024 * 1024
    server = MCPServer(
        "aep-stock",
        version="0.1.0",
        instructions=(
            "Searches Pixabay for commercially usable stock video and downloads the chosen "
            "clips to the media volume. Search first (with thumbnails for a vision model), "
            "then download only the clips you use. Downloaded files are file references "
            "under _work/stock/ carrying their source (site, page, author, license); save "
            "them into a media library with the tool node's save_media. Say the footage is "
            "from Pixabay wherever search results are shown."
        ),
    )

    async def thumbnail(clip: Clip) -> Any:
        url = clip.thumbnail()
        if url is None:
            return None
        target = folder.path / THUMBS / f"pixabay-{clip.number}.jpg"
        if not folder.fresh(target) and not await pixabay.image(url, target):
            return None
        return folder.ref(target, target.name)

    @server.tool(annotations=LOOKS)
    async def search_videos(
        query: Annotated[str, Field(min_length=1, max_length=100)],
        lang: Literal["en", "zh"] = "en",
        orientation: Literal["portrait", "landscape", "any"] = "any",
        min_short_side: Annotated[int, Field(ge=360, le=4320)] = 1080,
        min_seconds: Annotated[float, Field(ge=0, le=3600)] = 0,
        max_seconds: Annotated[float | None, Field(ge=0, le=3600)] = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 8,
        thumbnails: bool = False,
    ) -> dict[str, Any]:
        """Real (not animated), safe-search stock video on Pixabay matching ``query``
        (English by default, or ``lang`` zh). Only clips with a version whose short side
        is at least ``min_short_side`` px, of the ``orientation`` that version has, and
        ``min_seconds``-``max_seconds`` long; Pixabay's 100 most popular for the query are
        looked at. Answers {"site": "Pixabay", "items": [{"id", "site", "url" (the clip's
        page), "author", "tags", "seconds", "width", "height" (the version a download
        gets), "orientation", "thumbnail" (a file reference to a poster image when
        ``thumbnails``, else null)}], "total"}. The same search is answered from a cache
        for 24 hours."""
        if not query.strip():
            raise ToolError("搜索词不能是空的")
        folder.sweep()
        try:
            clips = await pixabay.search(query, lang)
        except StockError as exc:
            raise ToolError(str(exc)) from None
        kept = []
        for clip in clips:
            version = clip.version(min_short_side)
            if version is None or clip.seconds < min_seconds:
                continue
            if max_seconds is not None and clip.seconds > max_seconds:
                continue
            if orientation != "any" and version.orientation != orientation:
                continue
            kept.append(clip)
        chosen = kept[:limit]
        if thumbnails:
            images = await asyncio.gather(*(thumbnail(clip) for clip in chosen))
        else:
            images = [None] * len(chosen)
        return {
            "site": SITE,
            "items": [_found(c, min_short_side, i) for c, i in zip(chosen, images, strict=True)],
            "total": len(kept),
        }

    @server.tool(annotations=LOOKS)
    async def download_video(
        id: Annotated[str, Field(min_length=1, max_length=40)],
        ctx: Context,
        min_short_side: Annotated[int, Field(ge=360, le=4320)] = 1080,
    ) -> dict[str, Any]:
        """Download one clip found by search_videos (``id`` like pixabay:12345): the
        smallest version whose short side is at least ``min_short_side`` px. A long job
        (docs/api/connector-jobs.md) when it takes more than a few seconds; the result is
        {"file": file reference with its "source" (site, page, author, license), "width",
        "height", "seconds"}. A clip downloaded in the last 24 hours is not fetched
        again."""
        key = _key(ctx)
        known = jobs.find(key)
        if known is not None:
            await jobs.wait(known, settings.stock_wait_seconds)
            return jobs.envelope(known)
        match = ID.fullmatch(id.strip())
        if match is None:
            raise ToolError("id 要写成 pixabay:数字")
        folder.sweep()
        try:
            clip = await pixabay.clip(int(match.group(1)))
        except StockError as exc:
            raise ToolError(str(exc)) from None
        version = clip.version(min_short_side)
        if version is None:
            raise ToolError(f"这段视频没有短边 {min_short_side} 像素以上的版本")
        if version.size > max_bytes:
            raise ToolError(f"文件超过 {settings.stock_max_mb} MB，没有下载")
        target = folder.path / f"pixabay-{clip.number}-{version.width}x{version.height}.mp4"
        result = {
            "file": folder.ref(target, f"pixabay-{clip.number}.mp4", clip.source()),
            "width": version.width,
            "height": version.height,
            "seconds": clip.seconds,
        }
        if folder.fresh(target):
            return jobs.envelope(jobs.done(key, result))

        async def work(report: Any) -> dict[str, Any]:
            await pixabay.download(version.url, target, max_bytes, report)
            return result

        # The same key may have been handed in again while Pixabay was asked.
        known = jobs.find(key)
        if known is not None:
            return jobs.envelope(known)
        job = jobs.start(key, work)
        await jobs.wait(job, settings.stock_wait_seconds)
        if job.status == "error":
            raise ToolError(job.error or "下载失败")
        return jobs.envelope(job)

    @server.tool(annotations=LOOKS)
    async def aep_job_status(job_id: str) -> dict[str, Any]:
        """Where a download handed back as a job is, and its result when done."""
        try:
            return jobs.envelope(jobs.get(job_id))
        except StockError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=LOOKS)
    async def aep_job_cancel(job_id: str) -> dict[str, Any]:
        """Stop a download; the part already downloaded is removed."""
        try:
            return jobs.envelope(await jobs.cancel(job_id))
        except StockError as exc:
            raise ToolError(str(exc)) from None

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return server, jobs, pixabay


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings()
    server, _jobs, _pixabay = build(settings)
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.stock_mcp_allowed_hosts,
            allowed_origins=settings.stock_mcp_allowed_origins,
        ),
    )


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.stock_mcp_host, port=settings.stock_mcp_port)


if __name__ == "__main__":
    main()
