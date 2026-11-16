"""``render``: shots laid end to end, sound and burned subtitles, into one MP4.

Each shot is encoded on its own (video only, subtitles burned in) and cached by what
went into it, so changing one shot re-encodes only that one. The last pass joins the
shots without re-encoding them and mixes the sound: the voice-over, music and the
shots' own sound where it is turned up.
"""

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from aep_media.config import Settings
from aep_media.ffmpeg import FFmpeg
from aep_media.paths import CACHE, Media, MediaError
from aep_media.probe import SQUARE, inspect

Report = Callable[[float, str], None]

VERSION = "1"
QUALITY = {
    "draft": ("veryfast", 28),
    "standard": ("faster", 21),
    "high": ("slow", 18),
}
FITS = ("contain", "cover", "blur")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
# A clip may run this much past its source: the last frame is held.
HOLD = 0.5
FONT = "Noto Sans CJK SC"
SAMPLE_RATE = 48000
# Sources probed at the same time while checking a render.
PROBES = 4


@dataclass
class Clip:
    path: Path
    kind: str
    start: float
    duration: float
    hold: float
    fit: str
    focus: tuple[float, float]
    volume: float
    has_audio: bool
    source_duration: float | None


@dataclass
class Track:
    path: Path
    at: float
    start: float
    duration: float | None
    volume: float
    loop: bool
    fade_in: float
    fade_out: float
    source_duration: float


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Style:
    size: float
    color: str
    outline: float
    outline_color: str
    y: float
    max_chars: int
    bold: bool


@dataclass
class Spec:
    width: int
    height: int
    fps: int
    background: str
    clips: list[Clip]
    tracks: list[Track]
    cues: list[Cue]
    style: Style
    total: float
    name: str
    quality: str


# Reading the arguments ---------------------------------------------------------------


def _prefix(where: str) -> str:
    return f"{where}：" if where else ""


def _number(
    obj: dict[str, Any],
    key: str,
    where: str,
    *,
    default: float | None,
    lo: float,
    hi: float,
    integer: bool = False,
    above: bool = False,
) -> float | None:
    value = obj.get(key, default)
    if value is None:
        return None
    bad = isinstance(value, bool) or not isinstance(value, int | float)
    if not bad and integer and float(value) != int(value):
        bad = True
    if bad or math.isnan(value) or value < lo or value > hi or (above and value <= lo):
        low = f"大于 {lo:g}" if above else f"{lo:g}"
        kind = "整数" if integer else "数"
        raise MediaError(f"{_prefix(where)}{key} 要是 {low}–{hi:g} 之间的{kind}")
    return float(value)


def _color(obj: dict[str, Any], key: str, where: str, default: str) -> str:
    value = obj.get(key, default)
    if not isinstance(value, str) or not COLOR.match(value):
        raise MediaError(f"{_prefix(where)}{key} 要是 #RRGGBB 这样的颜色")
    return value.upper()


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MediaError(f"{what}要是一个对象")
    return value


def _name(output: dict[str, Any]) -> str:
    name = output.get("name", "成片.mp4")
    if (
        not isinstance(name, str)
        or not name.strip()
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name.strip().startswith(".")
        or PurePosixPath(name.strip()).suffix.lower() != ".mp4"
        or len(name.strip().encode()) > 200
    ):
        raise MediaError("output.name 要是 .mp4 文件名，不带路径，最多 200 字节")
    return name.strip()


async def _probe_all(
    media: Media, ff: FFmpeg, settings: Settings, items: list[Any]
) -> dict[Path, dict[str, Any] | MediaError]:
    """Each distinct source probed, a few at a time, within the time a check may take.

    A render must hand back its job before the platform's tool call times out, so the
    whole check is bounded; a source that can't be used keeps its error for the item."""
    paths: list[Path] = []
    for item in items:
        try:
            path = media.resolve(item.get("source") if isinstance(item, dict) else None)
        except MediaError:
            continue  # reported, with the item, when the item is read
        if path not in paths:
            paths.append(path)
    found: dict[Path, dict[str, Any] | MediaError] = {}
    turns = asyncio.Semaphore(PROBES)

    async def one(path: Path) -> None:
        async with turns:
            try:
                found[path] = await inspect(ff, path)
            except MediaError as exc:
                found[path] = exc

    try:
        async with asyncio.timeout(settings.media_check_seconds):
            async with asyncio.TaskGroup() as group:
                for path in paths:
                    group.create_task(one(path))
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None
    except TimeoutError:
        raise MediaError(
            f"检查素材超过 {settings.media_check_seconds:g} 秒还没完：素材太多，或者有文件读得太慢"
        ) from None
    return found


async def read_spec(media: Media, ff: FFmpeg, settings: Settings, args: dict[str, Any]) -> Spec:
    """The render arguments checked, with each source probed; errors name the item."""
    width = int(_number(args, "width", "", default=1080, lo=240, hi=3840, integer=True) or 0)
    height = int(_number(args, "height", "", default=1920, lo=240, hi=3840, integer=True) or 0)
    if width % 2 or height % 2:
        raise MediaError("width 和 height 要是偶数")
    fps = int(_number(args, "fps", "", default=30, lo=15, hi=60, integer=True) or 0)
    background = _color(args, "background", "", "#000000")
    raw_clips = args.get("clips")
    if not isinstance(raw_clips, list) or not 1 <= len(raw_clips) <= 300:
        raise MediaError("clips 要有 1–300 个镜头")
    raw_tracks = args.get("audio") or []
    if not isinstance(raw_tracks, list) or len(raw_tracks) > 10:
        raise MediaError("audio 最多 10 条音轨")
    probed = await _probe_all(media, ff, settings, [*raw_clips, *raw_tracks])

    async def look(source: Any, where: str) -> tuple[Path, dict[str, Any]]:
        try:
            path = media.resolve(source)
            found = probed.get(path)
            if found is None:
                found = probed[path] = await inspect(ff, path)
            if isinstance(found, MediaError):
                raise found
        except MediaError as exc:
            raise MediaError(f"{where}：{exc}") from None
        return path, found

    clips: list[Clip] = []
    for index, raw in enumerate(raw_clips):
        where = f"第 {index + 1} 个镜头"
        item = _object(raw, where)
        path, info = await look(item.get("source"), where)
        if info["kind"] == "audio":
            raise MediaError(f"{where}：要是视频或图片")
        fit = item.get("fit", "contain")
        if fit not in FITS:
            raise MediaError(f"{where}：fit 只能是 contain、cover、blur")
        focus = item.get("focus", [0.5, 0.5])
        if (
            not isinstance(focus, list)
            or len(focus) != 2
            or not all(
                isinstance(v, int | float) and not isinstance(v, bool) and 0 <= v <= 1
                for v in focus
            )
        ):
            raise MediaError(f"{where}：focus 要是两个 0–1 之间的数")
        volume = _number(item, "volume", where, default=0, lo=0, hi=2) or 0.0
        duration = _number(item, "duration", where, default=None, lo=0.2, hi=600)
        start = 0.0
        hold = 0.0
        source_duration = info["duration_seconds"]
        if info["kind"] == "image":
            if duration is None:
                raise MediaError(f"{where}：图片要写 duration")
        else:
            start = _number(item, "start", where, default=0, lo=0, hi=86400) or 0.0
            assert source_duration is not None
            if start >= source_duration:
                raise MediaError(f"{where}：start 超过了素材时长（{source_duration:g} 秒）")
            left = source_duration - start
            if duration is None:
                duration = left
                if duration < 0.2:
                    raise MediaError(f"{where}：从 {start:g} 秒起素材只剩不到 0.2 秒")
            elif duration > left + HOLD:
                raise MediaError(
                    f"{where}：素材只有 {source_duration:g} 秒，"
                    f"从 {start:g} 秒起截不出 {duration:g} 秒"
                )
            hold = max(0.0, duration - left)
        clips.append(
            Clip(
                path=path,
                kind=info["kind"],
                start=start,
                duration=duration,
                hold=hold,
                fit=fit,
                focus=(float(focus[0]), float(focus[1])),
                volume=volume,
                has_audio=bool(info["has_audio"]) and info["kind"] == "video",
                source_duration=source_duration,
            )
        )

    tracks: list[Track] = []
    for index, raw in enumerate(raw_tracks):
        where = f"第 {index + 1} 条音轨"
        item = _object(raw, where)
        path, info = await look(item.get("source"), where)
        if not info["has_audio"] or info["kind"] == "image":
            raise MediaError(f"{where}：要是音频或带声音的视频")
        source_duration = info["duration_seconds"] or 0.0
        start = _number(item, "start", where, default=0, lo=0, hi=86400) or 0.0
        if start >= source_duration:
            raise MediaError(f"{where}：start 超过了音轨时长（{source_duration:g} 秒）")
        loop = item.get("loop", False)
        if not isinstance(loop, bool):
            raise MediaError(f"{where}：loop 要是 true 或 false")
        tracks.append(
            Track(
                path=path,
                at=_number(item, "at", where, default=0, lo=0, hi=86400) or 0.0,
                start=start,
                duration=_number(item, "duration", where, default=None, lo=0, hi=86400, above=True),
                volume=_number(item, "volume", where, default=1, lo=0, hi=2) or 0.0,
                loop=loop,
                fade_in=_number(item, "fade_in", where, default=0, lo=0, hi=10) or 0.0,
                fade_out=_number(item, "fade_out", where, default=0, lo=0, hi=10) or 0.0,
                source_duration=source_duration,
            )
        )

    subtitles = args.get("subtitles")
    cues: list[Cue] = []
    style_args: dict[str, Any] = {}
    if subtitles is not None:
        subtitles = _object(subtitles, "subtitles ")
        raw_cues = subtitles.get("cues") or []
        if not isinstance(raw_cues, list) or len(raw_cues) > 2000:
            raise MediaError("subtitles.cues 最多 2000 条字幕")
        for index, raw in enumerate(raw_cues):
            where = f"第 {index + 1} 条字幕"
            item = _object(raw, where)
            start = _number(item, "start", where, default=None, lo=0, hi=86400)
            end = _number(item, "end", where, default=None, lo=0, hi=86400)
            if start is None or end is None:
                raise MediaError(f"{where}：要写 start 和 end")
            if end <= start:
                raise MediaError(f"{where}：end 要比 start 晚")
            text = item.get("text")
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 200:
                raise MediaError(f"{where}：text 要是 1–200 个字")
            cues.append(Cue(start, end, " ".join(text.split())))
        style_args = _object(subtitles.get("style") or {}, "subtitles.style ")
    where = "字幕样式"
    bold = style_args.get("bold", False)
    if not isinstance(bold, bool):
        raise MediaError(f"{where}：bold 要是 true 或 false")
    style = Style(
        size=_number(style_args, "size", where, default=60, lo=10, hi=200) or 60.0,
        color=_color(style_args, "color", where, "#FFFFFF"),
        outline=_number(style_args, "outline", where, default=3, lo=0, hi=20) or 0.0,
        outline_color=_color(style_args, "outline_color", where, "#000000"),
        y=_number(style_args, "y", where, default=0.82, lo=0, hi=1) or 0.0,
        max_chars=int(
            _number(style_args, "max_chars", where, default=16, lo=4, hi=60, integer=True) or 16
        ),
        bold=bold,
    )

    shots = sum(clip.duration for clip in clips)
    total = _number(args, "duration", "", default=None, lo=0, hi=86400, above=True) or shots
    if total > settings.media_max_seconds:
        raise MediaError(f"成片 {total:g} 秒，超过了 {settings.media_max_seconds} 秒的上限")
    clips = _fit_to(clips, total)
    output = _object(args.get("output") or {}, "output ")
    quality = output.get("quality", "standard")
    if quality not in QUALITY:
        raise MediaError("output.quality 只能是 draft、standard、high")
    return Spec(
        width=width,
        height=height,
        fps=fps,
        background=background,
        clips=clips,
        tracks=tracks,
        cues=[cue for cue in cues if cue.start < total],
        style=style,
        total=total,
        name=_name(output),
        quality=quality,
    )


def _fit_to(clips: list[Clip], total: float) -> list[Clip]:
    """Cut the shots at ``total``, or hold the last one longer to reach it."""
    kept: list[Clip] = []
    used = 0.0
    for clip in clips:
        if used >= total - 1e-6:
            break
        left = total - used
        if clip.duration > left:
            clip.duration = left
            if clip.source_duration is not None and clip.kind == "video":
                clip.hold = max(0.0, clip.duration - (clip.source_duration - clip.start))
        kept.append(clip)
        used += clip.duration
    if used < total - 1e-6:
        # The selected stretch stays as it is; its last frame is held for the rest.
        last = kept[-1]
        extra = total - used
        last.duration += extra
        if last.kind == "video":
            last.hold += extra
    return kept


# Subtitles ---------------------------------------------------------------------------

BREAK_AFTER = set("，。！？；：、,.!?;: ）)」』】》")


def _width(char: str) -> float:
    return 0.5 if ord(char) < 0x2E80 else 1.0


def wrap(text: str, max_chars: int) -> list[str]:
    """Lines of at most ``max_chars`` (Latin letters count half), as even as possible."""
    total = sum(_width(c) for c in text)
    if total <= max_chars:
        return [text]
    lines: list[str] = []
    rest = text
    while rest:
        width = sum(_width(c) for c in rest)
        if width <= max_chars:
            lines.append(rest)
            break
        count = math.ceil(width / max_chars)
        target = width / count
        best, best_score, used = 1, math.inf, 0.0
        for i, char in enumerate(rest[:-1], start=1):
            used += _width(char)
            if used > max_chars:
                break
            score = abs(used - target)
            if char not in BREAK_AFTER:
                score += 1.5
            if char.isascii() and char.isalnum() and rest[i].isascii() and rest[i].isalnum():
                score += 6
            # On a tie the first line is the longer one.
            if score <= best_score:
                best, best_score = i, score
        lines.append(rest[:best].rstrip())
        rest = rest[best:].lstrip()
    return lines


def _ass_color(hex_color: str) -> str:
    red, green, blue = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f"&H00{blue}{green}{red}".upper()


def _ass_time(seconds: float) -> str:
    centi = max(0, round(seconds * 100))
    hours, centi = divmod(centi, 360000)
    minutes, centi = divmod(centi, 6000)
    secs, centi = divmod(centi, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centi:02d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def ass(spec: Spec, start: float, length: float) -> str | None:
    """The subtitles shown in [start, start + length), in that shot's own time."""
    if not spec.cues:
        return None
    events = []
    scale = spec.width / 1080
    x, y = spec.width // 2, round(spec.style.y * spec.height)
    for cue in spec.cues:
        begin, end = max(cue.start, start), min(cue.end, start + length, spec.total)
        if end - begin < 0.01:
            continue
        text = "\\N".join(_ass_text(line) for line in wrap(cue.text, spec.style.max_chars))
        events.append(
            f"Dialogue: 0,{_ass_time(begin - start)},{_ass_time(end - start)},Default,,0,0,0,,"
            f"{{\\an5\\pos({x},{y})}}{text}"
        )
    if not events:
        return None
    style = spec.style
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {spec.width}",
        f"PlayResY: {spec.height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{FONT},{style.size * scale:.1f},{_ass_color(style.color)},"
        f"{_ass_color(style.color)},{_ass_color(style.outline_color)},&H00000000,"
        f"{-1 if style.bold else 0},0,0,0,100,100,0,0,1,{style.outline * scale:.1f},0,5,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    return "\n".join(head + events) + "\n"


# Encoding ----------------------------------------------------------------------------


def _fit_filter(spec: Spec, clip: Clip) -> str:
    width, height = spec.width, spec.height
    color = "0x" + spec.background[1:]
    if clip.fit == "cover":
        fx, fy = clip.focus
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-ow)*{fx:g}:(ih-oh)*{fy:g}"
        )
    inside = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if clip.fit == "contain":
        return f"{inside},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={color}"
    small_w, small_h = max(2, width // 16 * 2), max(2, height // 16 * 2)
    return (
        f"split=2[back][front];"
        f"[back]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
        f"crop={small_w}:{small_h},boxblur=6:2,scale={width}:{height},setsar=1[blurred];"
        f"[front]{inside},setsar=1[inside];"
        f"[blurred][inside]overlay=(W-w)/2:(H-h)/2"
    )


@dataclass
class Shot:
    clip: Clip
    start: float  # where it begins in the video, on a frame
    frames: int
    subtitles: str | None

    def seconds(self, fps: int) -> float:
        return self.frames / fps


def plan(spec: Spec) -> list[Shot]:
    """Shots on whole frames: each starts where the sum of the ones before rounds to."""
    shots: list[Shot] = []
    at = 0.0
    for clip in spec.clips:
        first = round(at * spec.fps)
        last = round((at + clip.duration) * spec.fps)
        at += clip.duration
        if last <= first:
            continue
        begin = first / spec.fps
        length = (last - first) / spec.fps
        shots.append(Shot(clip, begin, last - first, ass(spec, begin, length)))
    return shots


def _stat(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def shot_key(spec: Spec, shot: Shot) -> str:
    clip = shot.clip
    data = {
        "v": VERSION,
        "source": str(clip.path),
        "stat": _stat(clip.path),
        "start": round(clip.start, 4),
        "frames": shot.frames,
        "hold": round(clip.hold, 4),
        "fit": clip.fit,
        "focus": clip.focus,
        "size": [spec.width, spec.height, spec.fps],
        "background": spec.background,
        "quality": spec.quality,
        "subtitles": shot.subtitles,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def encode_shot(
    ff: FFmpeg, spec: Spec, shot: Shot, out: Path, progress: Callable[[float], None]
) -> None:
    clip = shot.clip
    seconds = shot.seconds(spec.fps)
    preset, crf = QUALITY[spec.quality]
    if clip.kind == "image":
        source = ["-loop", "1", "-framerate", str(spec.fps), "-t", f"{seconds + 1:.3f}"]
    else:
        # Only the selected stretch is read: a held shot freezes on its last frame
        # instead of running on into footage after it.
        read = seconds - clip.hold if clip.hold > 0 else seconds + 1
        source = ["-ss", f"{clip.start:.3f}", "-t", f"{max(read, 1 / spec.fps):.3f}"]
    chain = (
        f"[0:v]setpts=PTS-STARTPTS,fps={spec.fps},{SQUARE},{_fit_filter(spec, clip)},setsar=1,"
        f"format=yuv420p,tpad=stop_mode=clone:stop_duration={clip.hold + 1:.3f},"
        f"trim=end_frame={shot.frames},setpts=PTS-STARTPTS"
    )
    # Two renders may encode the same shot at once: each has its own working files.
    own = f"{out.stem}.{secrets.token_hex(4)}"
    subtitles = out.with_name(f"{own}.part.ass")
    if shot.subtitles:
        subtitles.write_text(shot.subtitles, encoding="utf-8")
        chain += f",ass={subtitles.name}"
    chain += "[v]"
    temporary = out.with_name(f"{own}.part.mp4")
    try:
        await ff.run(
            [
                *source,
                "-i",
                str(clip.path),
                "-filter_complex",
                chain,
                "-map",
                "[v]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(crf),
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-g",
                str(spec.fps * 2),
                *ff.encode_threads(),
                "-video_track_timescale",
                "90000",
                "-frames:v",
                str(shot.frames),
                "-f",
                "mp4",
                temporary.name,
            ],
            seconds=seconds,
            progress=progress,
            cwd=out.parent,
        )
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
        subtitles.unlink(missing_ok=True)


def _sound(
    path: Path, at: float, start: float, length: float, volume: float, loop: bool = False
) -> dict[str, Any]:
    return {
        "path": path,
        "at": at,
        "start": start,
        "length": length,
        "volume": volume,
        "loop": loop,
        "fade_in": 0.0,
        "fade_out": 0.0,
    }


def sounds(spec: Spec, shots: list[Shot]) -> list[dict[str, Any]]:
    """Every sound in the mix: the shots' own where turned up, then the tracks."""
    found = []
    for shot in shots:
        clip = shot.clip
        if clip.volume > 0 and clip.has_audio and clip.source_duration:
            played = shot.seconds(spec.fps) - clip.hold
            length = min(played, clip.source_duration - clip.start)
            if length > 0.01:
                found.append(_sound(clip.path, shot.start, clip.start, length, clip.volume))
    for track in spec.tracks:
        room = spec.total - track.at
        if room <= 0.01:
            continue
        if track.loop:
            length = room
        else:
            length = track.duration or (track.source_duration - track.start)
            length = min(length, track.source_duration - track.start, room)
        if length <= 0.01:
            continue
        sound = _sound(track.path, track.at, track.start, length, track.volume, track.loop)
        sound["fade_in"] = min(track.fade_in, length)
        sound["fade_out"] = min(track.fade_out, length)
        found.append(sound)
    return found


async def mix(
    ff: FFmpeg,
    spec: Spec,
    shots: list[Shot],
    files: list[Path],
    out: Path,
    progress: Callable[[float], None],
) -> None:
    listing = out.with_name("shots.txt")
    lines = ["ffconcat version 1.0"]
    for shot, file in zip(shots, files, strict=True):
        lines += [f"file '{file}'", f"duration {shot.seconds(spec.fps):.6f}"]
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    inputs = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    parts = []
    labels = []
    mixed = sounds(spec, shots)
    for index, sound in enumerate(mixed, start=1):
        if sound["loop"]:
            inputs += ["-stream_loop", "-1"]
        inputs += ["-ss", f"{sound['start']:.3f}", "-i", str(sound["path"])]
        length = sound["length"]
        chain = (
            f"[{index}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:"
            f"channel_layouts=stereo,atrim=end={length:.6f},asetpts=PTS-STARTPTS,"
            f"volume={sound['volume']:g}"
        )
        if sound["fade_in"]:
            chain += f",afade=t=in:st=0:d={sound['fade_in']:.3f}"
        if sound["fade_out"]:
            chain += f",afade=t=out:st={length - sound['fade_out']:.3f}:d={sound['fade_out']:.3f}"
        delay = round(sound["at"] * 1000)
        if delay:
            chain += f",adelay=delays={delay}:all=1"
        parts.append(f"{chain}[s{index}]")
        labels.append(f"[s{index}]")
    bed = len(mixed) + 1
    inputs += [
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
    ]
    parts.append(f"[{bed}:a]atrim=end={spec.total:.6f}[bed]")
    parts.append(
        f"[bed]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=first:normalize=0,"
        "alimiter=limit=0.95:level=disabled[sound]"
    )
    temporary = out.with_name(f".{secrets.token_hex(4)}.part.mp4")
    try:
        await ff.run(
            [
                *inputs,
                "-filter_complex",
                ";".join(parts),
                "-map",
                "0:v",
                "-map",
                "[sound]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                str(SAMPLE_RATE),
                "-t",
                f"{spec.total:.6f}",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(temporary),
            ],
            seconds=spec.total,
            progress=progress,
        )
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)


async def render_spec(
    media: Media, ff: FFmpeg, spec: Spec, task_dir: Path, report: Report
) -> dict[str, Any]:
    """Encode the shots (or take them from the cache) and mix them into ``task_dir``."""
    shots = plan(spec)
    cache = media.work / CACHE
    cache.mkdir(parents=True, exist_ok=True)
    total_frames = sum(shot.frames for shot in shots) or 1
    done_frames = 0
    files: list[Path] = []
    for index, shot in enumerate(shots):
        message = f"渲染第 {index + 1} 个镜头，共 {len(shots)} 个"
        before = done_frames

        def shot_progress(
            share: float, before: int = before, shot: Shot = shot, message: str = message
        ) -> None:
            report(0.9 * (before + share * shot.frames) / total_frames, message)

        report(0.9 * before / total_frames, message)
        file = cache / f"shot_{shot_key(spec, shot)}.mp4"
        if file.exists():
            os.utime(file)
        else:
            try:
                await encode_shot(ff, spec, shot, file, shot_progress)
            except MediaError as exc:
                raise MediaError(f"第 {index + 1} 个镜头：{exc}") from None
        files.append(file)
        done_frames += shot.frames
    report(0.9, "合成成片")
    out = task_dir / spec.name
    await mix(ff, spec, shots, files, out, lambda share: report(0.9 + 0.1 * share, "合成成片"))
    return {
        "video": media.file_ref(out, spec.name),
        "duration_seconds": round(spec.total, 3),
        "width": spec.width,
        "height": spec.height,
        "size_bytes": out.stat().st_size,
    }
