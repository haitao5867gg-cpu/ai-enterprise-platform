"""Images an ``llm`` node sends with its prompt to a model that can see them
(docs/api/vision.md): read from the media volume and put after the text as data URLs.

Imported at the end of ``nodes`` (like the other node modules), which calls these from the
``llm`` node's check and run."""

import asyncio
import base64
from pathlib import Path
from typing import Any

from aep.media.files import KINDS, MEDIA_TYPES, VolumeFileError, volume_file
from aep.runtime.flow import refs
from aep.runtime.flow.nodes import INPUT_ERROR, NodeFailure, Problems, Runtime

KEY = "images"
MAX_ENTRIES = 10
SHAPE = f"图片要写最多 {MAX_ENTRIES} 项，每项一个引用或媒体路径"
FILE_KEY = "aep_file"


def check_images(node: dict[str, Any], p: Problems) -> None:
    images = node.get(KEY)
    if images is None:
        return
    if not isinstance(images, list) or len(images) > MAX_ENTRIES:
        p.add((KEY,), SHAPE)
        return
    for i, entry in enumerate(images):
        if not isinstance(entry, str) or not entry.strip():
            p.add((KEY, i), SHAPE)


def media_files(value: Any) -> list[str]:
    """The media paths in a value, in order: file references and media summaries, also
    inside objects and lists; a string that is the value itself (or an item of a list that
    is) is a path."""
    found: list[str] = []

    def walk(node: Any, top: bool) -> None:
        if isinstance(node, str):
            if top and node.strip():
                found.append(node.strip())
        elif isinstance(node, dict):
            ref = node.get(FILE_KEY)
            if set(node) == {FILE_KEY} and isinstance(ref, dict):
                if isinstance(ref.get("path"), str):
                    found.append(ref["path"])
            elif isinstance(node.get("item_id"), str) and isinstance(node.get("path"), str):
                found.append(node["path"])
            else:
                for child in node.values():
                    walk(child, False)
        elif isinstance(node, list):
            for child in node:
                walk(child, top)

    walk(value, True)
    return found


def _read(root: Path, path: str, limit_mb: int) -> tuple[str, bytes]:
    try:
        file = volume_file(root, path)
    except VolumeFileError as exc:
        raise NodeFailure(INPUT_ERROR, str(exc)) from None
    suffix = file.suffix.lower()
    if KINDS.get(suffix) != "image":
        raise NodeFailure(INPUT_ERROR, f"只能发图片：{file.name}")
    if file.stat().st_size > limit_mb * 1024 * 1024:
        raise NodeFailure(INPUT_ERROR, f"图片「{file.name}」超过 {limit_mb} MB")
    return MEDIA_TYPES.get(suffix, "image/jpeg"), file.read_bytes()


async def image_parts(
    node: dict[str, Any], state: dict[str, Any], rt: Runtime
) -> list[dict[str, Any]]:
    """The node's images as message parts; [] when it sends none. All are found and read
    before anything is sent, and the run's policy must let images go to models."""
    entries = node.get(KEY) or []
    if not entries:
        return []
    lookup = rt.lookup(state)
    paths: list[str] = []
    for i, entry in enumerate(entries):
        value = refs.resolve(entry, lookup)
        if value is None or value == "" or value == []:
            continue  # an optional input left empty
        found = media_files(value)
        if not found:
            shown = refs.as_text(value)[:200]
            raise NodeFailure(INPUT_ERROR, f"第 {i + 1} 项图片取到的不是文件：{shown}")
        paths += found
    paths = list(dict.fromkeys(paths))  # the same file is sent once
    limit = rt.settings.vision_max_images
    if len(paths) > limit:
        raise NodeFailure(INPUT_ERROR, f"一次最多发 {limit} 张图片，取到了 {len(paths)} 张")
    read = [
        await asyncio.to_thread(_read, rt.settings.media_dir, path, rt.settings.vision_max_mb)
        for path in paths
    ]
    rt.media_check(node, state)("image")
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{kind};base64,{base64.b64encode(data).decode()}"},
        }
        for kind, data in read
    ]
