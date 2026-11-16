"""Automatic descriptions of media items (docs/api/media-search.md, 自动描述).

A library can ask for its videos and images to be described. Once an item is probed and
nobody has written its description, the describer takes a few frames with the media
processing connector's ``frames`` tool and has the library's vision model write one line
about what is seen. A person's description is never written over: the model's is kept only
while the description is still empty and nobody wrote it. While the connector or the
gateway cannot be reached, items wait and are tried again a minute later.
"""

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any

import openai
from openai import AsyncOpenAI

from aep import llm
from aep.config import Settings
from aep.media.files import KINDS, MEDIA_TYPES, VolumeFileError, volume_file
from aep.media.store import MediaItem, MediaStore
from aep.policy import MEDIA_LABELS, platform_default
from aep.tools.jobs import JobTimedOut, Watch, follow, is_job
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError, tool_message

log = logging.getLogger(__name__)

FRAMES_TOOL = "frames"
FRAME_COUNT = 4
FRAME_WIDTH = 512
MAX_DESCRIPTION = 500
RETRY_SECONDS = 60
BATCH = 20
# How long taking the frames may run when the connector hands back a job.
FRAMES_SECONDS = 120.0
FAILED = "写描述失败："
SYSTEM = (
    "你在给素材库里的视频和图片写画面描述，以后要按一句话找素材。"
    "只写画面里看得到的东西：谁（几个人、男女、大致年龄，看不到脸就写不露脸）、在做什么、"
    "在哪里、景别和角度（特写、全景、侧面……）、画面上有没有字幕或文字。"
    "用一句中文，不超过 80 个字，不推测，不评价，不加引号。"
)
ORIENTATIONS = {"portrait": "竖屏", "landscape": "横屏", "square": "方形"}
QUOTES = "\"'“”‘’「」『』"


class DescribeFailed(Exception):
    """The item cannot be described; the message says why."""


def clean(text: str) -> str:
    """A model's answer as a description: one line, no surrounding quotes, cut short."""
    line = re.sub(r"\s+", " ", text).strip().strip(QUOTES).strip()
    return line[:MAX_DESCRIPTION]


def _orientation(item: MediaItem) -> str | None:
    if not item.width or not item.height:
        return None
    if item.width == item.height:
        return "square"
    return "landscape" if item.width > item.height else "portrait"


def prompt(item: MediaItem, hint: str) -> str:
    """What the model is told about the item besides its frames."""
    shape = ORIENTATIONS.get(_orientation(item) or "", "")
    if item.kind == "video":
        length = f"{item.duration_seconds:g} 秒的" if item.duration_seconds else ""
        what = f"一段{length}{shape}视频，下面是按时间均匀取的几帧"
    else:
        what = f"一张{shape}图片"
    lines = [f"素材库的说明：{hint}"] if hint else []
    lines += [f"文件名：{item.name}", f"这是{what}。"]
    return "\n".join(lines)


def _image(root: Path, path: Any, limit_mb: int) -> dict[str, Any]:
    """One frame the connector made, as a message part."""
    try:
        file = volume_file(root, path)
    except VolumeFileError as exc:
        raise DescribeFailed(f"抽帧的结果不对：{exc}") from None
    suffix = file.suffix.lower()
    if KINDS.get(suffix) != "image":
        raise DescribeFailed(f"抽帧的结果不是图片：{file.name}")
    if file.stat().st_size > limit_mb * 1024 * 1024:
        raise DescribeFailed(f"抽出的帧「{file.name}」超过 {limit_mb} MB")
    data = base64.b64encode(file.read_bytes()).decode()
    kind = MEDIA_TYPES.get(suffix, "image/jpeg")
    return {"type": "image_url", "image_url": {"url": f"data:{kind};base64,{data}"}}


def frame_paths(found: Any) -> list[str]:
    """The files of the frames the ``frames`` tool returned, in order."""
    frames = found.get("frames") if isinstance(found, dict) else None
    if not isinstance(frames, list) or not frames:
        raise DescribeFailed("没有抽出帧")
    paths: list[str] = []
    for frame in frames:
        image = frame.get("image") if isinstance(frame, dict) else None
        ref = image.get("aep_file") if isinstance(image, dict) else None
        path = ref.get("path") if isinstance(ref, dict) else None
        if not isinstance(path, str) or not path:
            raise DescribeFailed("抽帧的结果格式不对")
        paths.append(path)
    return paths


class Describer:
    def __init__(
        self,
        store: MediaStore,
        tools: McpTools | None,
        settings: Settings,
        client: AsyncOpenAI,
    ) -> None:
        self.store = store
        self.tools = tools
        self.settings = settings
        self.client = client
        self._wake = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.media_connector_url) and self.tools is not None

    def kick(self) -> None:
        """Something may be waiting to be described: look now, not at the next round."""
        self._wake.set()

    async def frames(self, item: MediaItem) -> list[str] | None:
        """The frames to show the model; None while the connector is away."""
        assert self.tools is not None
        url = self.settings.media_connector_url
        args: dict[str, Any] = {"source": item.rel_path, "width": FRAME_WIDTH}
        if item.kind == "video":
            args["count"] = FRAME_COUNT
        try:
            found = await self.tools.call(
                url, FRAMES_TOOL, args, self.settings.tool_timeout_seconds
            )
            if is_job(found):
                found = await follow(
                    self.tools,
                    url,
                    found,
                    Watch(),
                    call_seconds=None,
                    limit_seconds=FRAMES_SECONDS,
                )
        except ConnectorUnavailable as exc:
            log.info("media connector unavailable, %s waits: %s", item.item_id, exc)
            return None
        except ToolError as exc:
            raise DescribeFailed(f"抽帧失败：{tool_message(exc, FRAMES_TOOL)}") from None
        except JobTimedOut as exc:
            raise DescribeFailed(f"抽帧失败：{exc}") from None
        return frame_paths(found)

    async def describe(self, item: MediaItem, describe: dict[str, Any]) -> MediaItem | None:
        """Describe one item and keep what the model wrote unless a person wrote first;
        None while the connector or the gateway is away."""
        try:
            return await self._describe(item, describe)
        except DescribeFailed as exc:
            updated = await self.store.update_item(
                item.library_id, item.item_id, describe_error=f"{FAILED}{exc}"[:MAX_DESCRIPTION]
            )
            return updated or item

    async def _describe(self, item: MediaItem, describe: dict[str, Any]) -> MediaItem | None:
        model = str(describe.get("model") or "")
        policy = platform_default(self.settings)
        if not policy.allows(model):
            allowed = "、".join(policy.models or ())
            raise DescribeFailed(f"平台只允许用模型 {allowed}，自动描述用的是 {model}")
        if "image" not in policy.media:
            raise DescribeFailed(f"平台不允许把{MEDIA_LABELS['image']}发给模型")
        paths = await self.frames(item)
        if paths is None:
            return None
        root, limit = self.settings.media_dir, self.settings.vision_max_mb
        images = [await asyncio.to_thread(_image, root, path, limit) for path in paths]
        text = prompt(item, str(describe.get("hint") or ""))
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": text}, *images]},
        ]
        try:
            reply = await llm.chat(self.client, messages, model=model)
        except openai.APIConnectionError as exc:
            # The gateway is away (a timeout is a connection error too): try again later.
            log.info("gateway unavailable, %s waits: %s", item.item_id, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - any other failure is the model's
            raise DescribeFailed(f"模型报错：{exc}") from None
        description = clean(reply.text)
        if not description:
            raise DescribeFailed("模型的回答是空的")
        written = await self.store.describe_item(item.library_id, item.item_id, description)
        return written or item

    async def sweep(self) -> None:
        """Describe what is waiting, oldest first, until the connector or the gateway
        cannot be reached."""
        if not self.enabled:
            return
        while True:
            waiting = await self.store.undescribed(BATCH)
            for item, describe in waiting:
                if await self.describe(item, describe) is None:
                    return
            if len(waiting) < BATCH:
                return

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 - keep describing later
                log.exception("describing media items failed")
            try:
                await asyncio.wait_for(self._wake.wait(), RETRY_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
