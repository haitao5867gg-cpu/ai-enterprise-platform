"""Pixabay's video API (https://pixabay.com/api/docs/, "Search Videos").

Pixabay's rules, followed here: answers are cached for 24 hours (the same search is not
asked again within that time); files are downloaded to our own server and used from
there, never linked to; only chosen clips are downloaded. The API key travels as a query
parameter, so no request URL is ever logged or put in an error.
"""

import logging
import os
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from aep_stock.config import Settings

# httpx logs every request URL at INFO, and Pixabay's carries the key.
logging.getLogger("httpx").setLevel(logging.WARNING)

SITE = "Pixabay"
LICENSE = "Pixabay Content License"
LICENSE_URL = "https://pixabay.com/service/license-summary/"
SIZES = ("tiny", "small", "medium", "large")
PER_PAGE = 100  # one request per search: the first 100 popular clips, filtered here
CACHE_LIMIT = 2000
IMAGE_LIMIT = 5 * 1024 * 1024
NO_KEY = "没有配置 Pixabay 的 API key（AEP_STOCK_PIXABAY_KEY）"

Report = Callable[[float, str], None]


class StockError(ValueError):
    """A problem to report to the caller as a tool error, in its words."""


@dataclass(frozen=True)
class Version:
    """One size of a clip: Pixabay's tiny, small, medium or large."""

    url: str
    width: int
    height: int
    size: int
    thumbnail: str | None

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    @property
    def orientation(self) -> str:
        if self.height > self.width:
            return "portrait"
        return "landscape" if self.width > self.height else "square"


@dataclass(frozen=True)
class Clip:
    number: int
    page: str
    author: str
    tags: tuple[str, ...]
    seconds: int
    versions: tuple[Version, ...]

    @property
    def id(self) -> str:
        return f"pixabay:{self.number}"

    def version(self, min_short_side: int) -> Version | None:
        """The smallest size at least this sharp: enough for the cut, nothing bigger."""
        sharp = [v for v in self.versions if v.short_side >= min_short_side]
        return min(sharp, key=lambda v: (v.short_side, v.size), default=None)

    def thumbnail(self) -> str | None:
        """A small poster image, for a vision model to look at."""
        for version in self.versions:  # smallest first
            if version.thumbnail and version.short_side >= 360:
                return version.thumbnail
        return next((v.thumbnail for v in reversed(self.versions) if v.thumbnail), None)

    def source(self) -> dict[str, Any]:
        """Where a downloaded file came from, as the platform records it."""
        return {
            "kind": "stock",
            "site": SITE,
            "url": self.page,
            "author": self.author or None,
            "license": LICENSE,
            "license_url": LICENSE_URL,
        }


def _number(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _web(url: Any) -> str | None:
    """A download address on Pixabay's servers; anything else is not followed."""
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "pixabay.com" or host.endswith(".pixabay.com")):
        return None
    return url


def read_clip(hit: Any) -> Clip | None:
    """One search hit; None when it is not a usable video."""
    if not isinstance(hit, dict):
        return None
    number = _number(hit.get("id"))
    page = hit.get("pageURL")
    if not number or not isinstance(page, str) or not page.startswith("https://"):
        return None
    versions = []
    for name in SIZES:
        raw = (hit.get("videos") or {}).get(name)
        if not isinstance(raw, dict) or not (url := _web(raw.get("url"))):
            continue  # large is often missing: an empty url and size 0
        width, height = _number(raw.get("width")), _number(raw.get("height"))
        if width and height:
            versions.append(
                Version(url, width, height, _number(raw.get("size")), _web(raw.get("thumbnail")))
            )
    if not versions:
        return None
    tags = tuple(t.strip() for t in str(hit.get("tags") or "").split(",") if t.strip())
    return Clip(
        number=number,
        page=page,
        author=str(hit.get("user") or "")[:200],
        tags=tags[:20],
        seconds=_number(hit.get("duration")),
        versions=tuple(sorted(versions, key=lambda v: (v.short_side, v.size))),
    )


class Pixabay:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.key = settings.stock_pixabay_key.get_secret_value().strip()
        self.url = settings.stock_pixabay_url
        self.keep = settings.stock_cache_hours * 3600
        self.clock = clock
        self.http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=True,
            headers={"User-Agent": "aep-stock/0.1"},
        )
        self.answers: OrderedDict[tuple[Any, ...], tuple[float, list[Clip]]] = OrderedDict()

    async def close(self) -> None:
        await self.http.aclose()

    def _hide(self, text: str) -> str:
        return text.replace(self.key, "***") if self.key else text

    async def search(self, query: str, lang: str) -> list[Clip]:
        """Real footage (no animation), safe search, the 100 most popular clips."""
        words = " ".join(query.split())
        params = {
            "q": words,
            "lang": lang,
            "video_type": "film",
            "safesearch": "true",
            "order": "popular",
            "per_page": str(PER_PAGE),
        }
        return await self._ask(("q", lang, words.lower()), params)

    async def clip(self, number: int) -> Clip:
        found = await self._ask(("id", number), {"id": str(number)})
        if not found:
            raise StockError(f"Pixabay 上没有这段视频了：pixabay:{number}")
        return found[0]

    async def _ask(self, key: tuple[Any, ...], params: dict[str, str]) -> list[Clip]:
        now = self.clock()
        cached = self.answers.get(key)
        if cached is not None and now - cached[0] < self.keep:
            return cached[1]
        if not self.key:
            raise StockError(NO_KEY)
        try:
            resp = await self.http.get(self.url, params={"key": self.key, **params})
        except httpx.HTTPError as exc:
            raise StockError(f"连不上 Pixabay：{self._hide(_reason(exc))}") from None
        text = self._hide(resp.text[:300])
        if resp.status_code == 429:
            raise StockError("Pixabay 请求太频繁，过一分钟再试")
        if resp.status_code >= 500:
            raise StockError(f"连不上 Pixabay：Pixabay 回答 {resp.status_code}")
        if resp.status_code in (400, 401, 403) and "api key" in text.lower():
            raise StockError("Pixabay 不认这个 API key")
        if resp.status_code != 200:
            if "id" in params and resp.status_code in (400, 404):
                raise StockError(f"Pixabay 上没有这段视频了：pixabay:{params['id']}")
            raise StockError(f"Pixabay 回答出错（{resp.status_code}）：{text}")
        try:
            hits = resp.json()["hits"]
        except (ValueError, KeyError, TypeError):
            raise StockError("Pixabay 的回答看不懂") from None
        clips = [clip for hit in hits or [] if (clip := read_clip(hit)) is not None]
        self.answers[key] = (now, clips)
        self.answers.move_to_end(key)
        while len(self.answers) > CACHE_LIMIT:
            self.answers.popitem(last=False)
        return clips

    async def download(self, url: str, target: Path, max_bytes: int, report: Report) -> None:
        """Stream a clip to ``target`` (via a part file, removed if anything stops it)."""
        mb = max_bytes // (1024 * 1024)
        part = target.with_name(f"{target.name}.part-{secrets.token_hex(4)}")
        try:
            async with self.http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise StockError(f"从 Pixabay 下载失败：回答 {resp.status_code}")
                total = _number(resp.headers.get("content-length"))
                if total > max_bytes:
                    raise StockError(f"文件超过 {mb} MB，没有下载")
                got = 0
                with part.open("wb") as out:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        got += len(chunk)
                        if got > max_bytes:
                            raise StockError(f"文件超过 {mb} MB，没有下载")
                        out.write(chunk)
                        if total:
                            report(got / total, f"已下载 {got / 1e6:.1f} / {total / 1e6:.1f} MB")
            if got == 0:
                raise StockError("从 Pixabay 下载到的是空文件")
            os.replace(part, target)
        except httpx.HTTPError as exc:
            raise StockError(f"从 Pixabay 下载失败：{self._hide(_reason(exc))}") from None
        finally:
            part.unlink(missing_ok=True)

    async def image(self, url: str, target: Path) -> bool:
        """A small poster image; False when it could not be had."""
        try:
            resp = await self.http.get(url)
        except httpx.HTTPError:
            return False
        if resp.status_code != 200 or not resp.content or len(resp.content) > IMAGE_LIMIT:
            return False
        part = target.with_name(f"{target.name}.part-{secrets.token_hex(4)}")
        try:
            part.write_bytes(resp.content)
            os.replace(part, target)
        except OSError:
            part.unlink(missing_ok=True)
            return False
        return True


def _reason(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "超时"
    return str(exc) or type(exc).__name__
