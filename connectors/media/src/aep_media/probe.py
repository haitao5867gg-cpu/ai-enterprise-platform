"""``probe``: what a file is, how long and how big, and a thumbnail."""

from pathlib import Path
from typing import Any

from aep_media.ffmpeg import FFmpeg
from aep_media.paths import Media, MediaError, kind_of

WHAT = {"video": "视频", "audio": "音频", "image": "图片"}
THUMBNAIL_EDGE = 480


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None


def _rate(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    top, _, bottom = value.partition("/")
    try:
        rate = float(top) / float(bottom)
    except (ValueError, ZeroDivisionError):
        return None
    return round(rate, 3) if rate > 0 else None


def _rotation(stream: dict[str, Any]) -> int:
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and "rotation" in side:
            try:
                return int(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int((stream.get("tags") or {}).get("rotate", 0)) % 360
    except (TypeError, ValueError):
        return 0


def orientation(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    if width == height:
        return "square"
    return "portrait" if height > width else "landscape"


async def inspect(ff: FFmpeg, path: Path) -> dict[str, Any]:
    """The probe answer without a thumbnail; raises MediaError for files it can't use."""
    kind = kind_of(path.name)
    if kind is None:
        raise MediaError(f"不支持这种文件：{path.name}")
    what = WHAT[kind]
    data = await ff.probe(path, what)
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    pictures = [
        s
        for s in streams
        if s.get("codec_type") == "video"
        and not (s.get("disposition") or {}).get("attached_pic")
        and s.get("width")
        and s.get("height")
    ]
    sounds = [s for s in streams if s.get("codec_type") == "audio"]
    picture = pictures[0] if pictures else None
    sound = sounds[0] if sounds else None
    if kind in ("video", "image") and picture is None:
        raise MediaError(f"不是有效的{what}：没有画面")
    if kind == "audio" and sound is None:
        raise MediaError("不是有效的音频：没有声音")
    width = height = None
    if kind != "audio" and picture is not None:
        width, height = int(picture["width"]), int(picture["height"])
        if _rotation(picture) in (90, 270):
            width, height = height, width
    duration = None
    if kind != "image":
        duration = _number((data.get("format") or {}).get("duration"))
        if duration is None:
            duration = _number((picture or sound or {}).get("duration"))
        if not duration:
            raise MediaError(f"不是有效的{what}：读不出时长")
        duration = round(duration, 3)
    fps = None
    if kind == "video" and picture is not None:
        fps = _rate(picture.get("avg_frame_rate")) or _rate(picture.get("r_frame_rate"))
    return {
        "kind": kind,
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "orientation": orientation(width, height),
        "fps": fps,
        "has_video": picture is not None and kind != "audio",
        "has_audio": sound is not None,
        "video_codec": picture.get("codec_name") if picture and kind != "audio" else None,
        "audio_codec": sound.get("codec_name") if sound else None,
        "size_bytes": path.stat().st_size,
    }


async def snapshot(
    ff: FFmpeg, path: Path, at: float, out: Path, *, width: int | None = None
) -> tuple[int, int]:
    """One frame at ``at`` seconds as a JPG: ``width`` wide, or 480 on the long edge."""
    if width:
        scale = f"scale={width}:-2"
    else:
        edge = THUMBNAIL_EDGE
        scale = f"scale='if(gte(iw,ih),{edge},-2)':'if(gte(iw,ih),-2,{edge})'"
    # Near the end of a video a seek can land after the last frame: try a little earlier.
    for when in (at, max(0.0, at - 1.0)):
        seek = ["-ss", f"{when:.3f}"] if when > 0 else []
        await ff.run(
            [*seek, "-i", str(path), "-frames:v", "1", "-vf", scale, "-q:v", "3", str(out)],
            timeout=60,
        )
        if out.exists() and out.stat().st_size:
            return jpeg_size(out)
    raise MediaError(f"取不出第 {at:g} 秒的画面")


def jpeg_size(path: Path) -> tuple[int, int]:
    """Width and height from a JPG's frame header."""
    data = path.read_bytes()
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + length
    raise MediaError("取出的画面读不出大小")


async def probe(media: Media, ff: FFmpeg, source: Any, thumbnail: bool) -> dict[str, Any]:
    path = media.resolve(source)
    found = await inspect(ff, path)
    found["thumbnail"] = None
    if thumbnail and found["has_video"]:
        duration = found["duration_seconds"] or 0
        at = 1.0 if duration > 1 else duration / 2
        out = media.task_dir() / "thumbnail.jpg"
        await snapshot(ff, path, at, out)
        found["thumbnail"] = media.file_ref(out)
    return found
