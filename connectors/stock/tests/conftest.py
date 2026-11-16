"""A fake Pixabay (API and CDN) in process, so no test reaches the real site."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from aep_stock.config import Settings

KEY = "good-key-123"
JPG = b"\xff\xd8\xff\xe0" + b"jpg" * 50


def sizes(*found: tuple[str, int, int]) -> dict[str, dict[str, Any]]:
    return {name: {"width": w, "height": h} for name, w, h in found}


# Clip number -> (tags, seconds, sizes). Bytes served are 1000 x the short side.
CLIPS: dict[int, tuple[str, int, dict[str, dict[str, Any]]]] = {
    # Filmed upright: a 1080 x 1920 medium version.
    1: (
        "woman, cooking, kitchen",
        14,
        sizes(
            ("large", 2160, 3840), ("medium", 1080, 1920), ("small", 720, 1280), ("tiny", 540, 960)
        ),
    ),  # fmt: skip
    # Landscape, with a 4K large version.
    2: (
        "running, treadmill, gym",
        30,
        sizes(
            ("large", 3840, 2160), ("medium", 1920, 1080), ("small", 1280, 720), ("tiny", 960, 540)
        ),
    ),  # fmt: skip
    # An old one: 720p at best, no large version.
    3: ("cooking, pan", 8, sizes(("medium", 1280, 720), ("small", 960, 540), ("tiny", 640, 360))),
    # Square.
    4: ("cooking, bowl", 5, sizes(("medium", 1080, 1080), ("small", 720, 720))),
    # Downloads slowly.
    5: ("slow, river", 20, sizes(("medium", 1080, 1920), ("small", 720, 1280))),
}


class FakePixabay:
    def __init__(self) -> None:
        self.api_calls: list[dict[str, str]] = []
        self.downloads: list[str] = []
        self.broken_thumbs = {2}
        self.gone: set[int] = set()
        self.fail: Exception | None = None
        self.release = asyncio.Event()

    def hit(self, number: int) -> dict[str, Any]:
        tags, seconds, found = CLIPS[number]
        videos = {}
        for name in ("large", "medium", "small", "tiny"):
            if name not in found:
                videos[name] = {"url": "", "width": 0, "height": 0, "size": 0, "thumbnail": ""}
                continue
            w, h = found[name]["width"], found[name]["height"]
            base = f"https://cdn.pixabay.com/video/2024/01/01/{number}-1_{name}"
            videos[name] = {
                "url": f"{base}.mp4",
                "width": w,
                "height": h,
                "size": 1000 * min(w, h),
                "thumbnail": f"{base}.jpg",
            }
        return {
            "id": number,
            "pageURL": f"https://pixabay.com/videos/id-{number}/",
            "type": "film",
            "tags": tags,
            "duration": seconds,
            "videos": videos,
            "user": f"作者{number}",
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            raise self.fail
        url = request.url
        if url.host == "pixabay.com" and url.path == "/api/videos/":
            params = dict(url.params)
            self.api_calls.append(params)
            if params.get("key") != KEY:
                return httpx.Response(400, text="[ERROR 400] Invalid or missing API key")
            q = params.get("q", "")
            if q == "busy":
                return httpx.Response(429, text="Too many requests")
            if q == "down":
                return httpx.Response(503, text="unavailable")
            if q == "broken":
                return httpx.Response(200, text="not json")
            if "id" in params:
                number = int(params["id"])
                found = [] if number not in CLIPS or number in self.gone else [number]
            else:
                words = set(q.lower().split())
                found = [n for n, (tags, _, _) in CLIPS.items() if words & set(tags.split(", "))]
            hits = [self.hit(n) for n in found]
            return httpx.Response(200, json={"total": len(hits), "totalHits": len(hits),
                                             "hits": hits})  # fmt: skip
        if url.host == "cdn.pixabay.com":
            name = url.path.rsplit("/", 1)[-1]
            number = int(name.split("-", 1)[0])
            size = name.rsplit("_", 1)[-1].split(".")[0]
            if name.endswith(".jpg"):
                if number in self.broken_thumbs:
                    return httpx.Response(404)
                return httpx.Response(200, content=JPG)
            found = CLIPS[number][2][size]
            total = 1000 * min(found["width"], found["height"])
            self.downloads.append(name)
            if number == 5:
                return httpx.Response(
                    200, headers={"content-length": str(total)}, content=self.slowly(total)
                )
            return httpx.Response(200, content=b"\x00" * total)
        return httpx.Response(404)

    async def slowly(self, total: int) -> AsyncIterator[bytes]:
        sent = 0
        while sent < total:
            if sent >= total // 2:
                await self.release.wait()  # half, then wait until the test lets it finish
            chunk = min(100_000, total - sent)
            sent += chunk
            await asyncio.sleep(0.01)
            yield b"\x00" * chunk


@pytest.fixture
def fake() -> FakePixabay:
    return FakePixabay()


@pytest.fixture
def transport(fake) -> httpx.MockTransport:
    return httpx.MockTransport(fake.handle)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(media_dir=tmp_path, stock_pixabay_key=KEY, stock_wait_seconds=2)


def work(root: Path) -> Path:
    return root / "_work" / "stock"
