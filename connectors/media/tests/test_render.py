import re
import subprocess
from pathlib import Path

import pytest
from conftest import path_of

from aep_media.config import Settings
from aep_media.paths import MediaError
from aep_media.probe import inspect
from aep_media.render import plan, read_spec, render_spec, wrap

SMALL = {"width": 360, "height": 640, "fps": 30, "output": {"name": "成片.mp4", "quality": "draft"}}


def pixels(path: Path, at: float, width: int, height: int) -> bytes:
    """The frame at ``at`` seconds as raw RGB."""
    return subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{at}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],  # fmt: skip
        check=True,
        capture_output=True,
    ).stdout[: width * height * 3]


def loudness(path: Path, start: float, seconds: float) -> float:
    """Mean volume in dB of a stretch of the sound; about -91 for silence."""
    err = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-ss",
            f"{start}",
            "-t",
            f"{seconds}",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],  # fmt: skip
        capture_output=True,
        text=True,
    ).stderr
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", err).group(1))


def frame_count(path: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "csv=p=0",
            str(path),
        ],  # fmt: skip
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(out.strip())


async def run(media, ff, settings, args, task="j_test"):
    spec = await read_spec(media, ff, settings, args)
    reports = []
    result = await render_spec(
        media, ff, spec, media.task_dir(task), lambda p, m: reports.append((p, m))
    )
    return result, reports, media.root / result["video"]["aep_file"]["path"]


async def test_three_fits_an_image_and_shots_on_whole_frames(root, media, ff, settings):
    video = path_of(root, "横屏.mp4")
    args = {
        **SMALL,
        "clips": [
            {"source": video, "start": 0.5, "duration": 1.01},
            {"source": {"path": path_of(root, "竖拍.mp4")}, "duration": 1.02, "fit": "cover"},
            {"source": path_of(root, "图.png"), "duration": 1.03, "fit": "blur"},
            {"source": {"aep_file": {"path": video}}, "start": 3, "duration": 0.7},
        ],
    }
    result, reports, out = await run(media, ff, settings, args)
    assert result["video"]["aep_file"] == {"path": "_work/j_test/成片.mp4", "name": "成片.mp4"}
    assert (result["width"], result["height"]) == (360, 640)
    assert result["duration_seconds"] == pytest.approx(3.76)
    found = await inspect(ff, out)
    assert (found["width"], found["height"], found["fps"]) == (360, 640, 30.0)
    assert found["has_audio"] and found["duration_seconds"] == pytest.approx(3.76, abs=0.05)
    # 3.76 s at 30 fps: 113 frames, however the shots' lengths round.
    assert frame_count(out) == round(3.76 * 30)
    assert [m for _, m in reports][0] == "渲染第 1 个镜头，共 4 个"
    assert reports[-1][1] == "合成成片" and reports[-1][0] == pytest.approx(1, abs=0.01)
    assert [p for p, _ in reports] == sorted(p for p, _ in reports)
    # contain leaves the background above a landscape shot; cover fills the frame.
    top = pixels(out, 0.5, 360, 640)[: 360 * 3 * 50]
    assert set(top) == {0} or max(top) < 20
    covered = pixels(out, 1.5, 360, 640)[: 360 * 3 * 50]
    assert max(covered) > 100


def test_shots_start_on_frames_and_add_up():
    class Clip:
        def __init__(self, duration):
            self.duration = duration

    class Spec:
        fps = 30
        clips = [Clip(0.33), Clip(0.33), Clip(0.34), Clip(1.0)]
        cues = []
        total = 2.0

    shots = plan(Spec())  # type: ignore[arg-type]
    assert [shot.frames for shot in shots] == [10, 10, 10, 30]
    assert sum(shot.frames for shot in shots) == 60
    assert [shot.start for shot in shots] == [0, 10 / 30, 20 / 30, 1.0]


async def test_a_short_source_is_held_a_little_and_refused_beyond(root, media, ff, settings):
    video = path_of(root, "横屏.mp4")
    held, _, out = await run(
        media, ff, settings, {**SMALL, "clips": [{"source": video, "start": 3.5, "duration": 0.9}]}
    )
    assert held["duration_seconds"] == pytest.approx(0.9)
    assert frame_count(out) == 27
    with pytest.raises(MediaError, match="第 2 个镜头：素材只有 4 秒，从 3 秒起截不出 2 秒"):
        clips = [{"source": video, "duration": 1}, {"source": video, "start": 3, "duration": 2}]
        await read_spec(media, ff, settings, {**SMALL, "clips": clips})


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"clips": []}, "clips 要有 1–300 个镜头"),
        ({"clips": [{"source": "IMAGE"}]}, "第 1 个镜头：图片要写 duration"),
        ({"clips": [{"source": "AUDIO", "duration": 1}]}, "第 1 个镜头：要是视频或图片"),
        ({"clips": [{"source": "VIDEO", "fit": "stretch"}]}, "fit 只能是"),
        ({"clips": [{"source": "VIDEO", "duration": 0.1}]}, "duration 要是 0.2–600"),
        ({"clips": [{"source": "VIDEO", "start": 9}]}, "start 超过了素材时长"),
        ({"clips": [{"source": "VIDEO", "focus": [2, 0]}]}, "focus 要是两个"),
        ({"clips": [{"source": "VIDEO"}], "width": 361}, "要是偶数"),
        ({"clips": [{"source": "VIDEO"}], "fps": 5}, "fps 要是 15–60"),
        ({"clips": [{"source": "VIDEO"}], "background": "black"}, "#RRGGBB"),
        ({"clips": [{"source": "VIDEO"}], "audio": [{"source": "IMAGE"}]}, "第 1 条音轨"),
        ({"clips": [{"source": "VIDEO"}], "audio": [{"source": "SILENT"}]}, "带声音的视频"),
        (
            {
                "clips": [{"source": "VIDEO"}],
                "subtitles": {"cues": [{"start": 2, "end": 1, "text": "x"}]},
            },
            "第 1 条字幕：end 要比 start 晚",
        ),  # fmt: skip
        ({"clips": [{"source": "VIDEO"}], "output": {"name": "成片.mov"}}, "output.name"),
        ({"clips": [{"source": "VIDEO"}], "output": {"quality": "best"}}, "output.quality"),
        ({"clips": [{"source": "VIDEO"}], "duration": 900}, "超过了 600 秒的上限"),
    ],
)
async def test_bad_arguments_name_the_item(root, media, ff, settings, change, error):
    names = {
        "VIDEO": "横屏.mp4",
        "IMAGE": "图.png",
        "AUDIO": "配音.m4a",
        "SILENT": "无声.mp4",
    }

    def fill(value):
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fill(v) for v in value]
        return path_of(root, names[value]) if value in names else value

    with pytest.raises(MediaError, match=error):
        await read_spec(media, ff, settings, fill({**SMALL, **change}))


async def test_the_video_is_limited_in_length(root, media, ff):
    settings = Settings(media_dir=root, media_max_seconds=5)
    video = path_of(root, "横屏.mp4")
    with pytest.raises(MediaError, match="成片 8 秒，超过了 5 秒的上限"):
        await read_spec(media, ff, settings, {**SMALL, "clips": [{"source": video}] * 2})


async def test_duration_cuts_the_shots_or_holds_the_last(root, media, ff, settings):
    video = path_of(root, "横屏.mp4")
    clips = [{"source": video, "duration": 1}, {"source": video, "start": 1, "duration": 1}]
    cut = await read_spec(media, ff, settings, {**SMALL, "clips": clips, "duration": 1.5})
    assert [c.duration for c in cut.clips] == [1, 0.5]
    held = await read_spec(
        media, ff, settings, {**SMALL, "clips": [{"source": video, "start": 2}], "duration": 3}
    )
    assert (held.clips[0].duration, held.clips[0].hold) == (3, 1)


async def test_sound_tracks_start_loop_and_stop(root, media, ff, settings):
    video = path_of(root, "无声.mp4")
    args = {
        **SMALL,
        "clips": [{"source": video, "duration": 3}],
        "audio": [{"source": path_of(root, "鼓点.wav"), "at": 1.5, "loop": True}],
    }
    _, _, out = await run(media, ff, settings, args)
    assert loudness(out, 0.1, 1.2) < -80
    assert loudness(out, 1.6, 0.3) > -40
    # Looped past the 1 s beat until the end of the video.
    assert loudness(out, 2.7, 0.25) > -40
    once = {**args, "audio": [{"source": path_of(root, "鼓点.wav"), "at": 0.5}]}
    _, _, out = await run(media, ff, settings, once, task="j_once")
    assert loudness(out, 0.6, 0.8) > -40
    assert loudness(out, 1.7, 1.0) < -80
    # A shot's own sound is off unless turned up.
    muted, _, out = await run(
        media, ff, settings,
        {**SMALL, "clips": [{"source": path_of(root, "横屏.mp4"), "duration": 1}]},
        task="j_muted",
    )  # fmt: skip
    assert loudness(out, 0.1, 0.8) < -80
    _, _, out = await run(
        media, ff, settings,
        {**SMALL, "clips": [{"source": path_of(root, "横屏.mp4"), "duration": 1, "volume": 1}]},
        task="j_loud",
    )  # fmt: skip
    assert loudness(out, 0.1, 0.8) > -40


async def test_subtitles_are_burned_in(root, media, ff, settings):
    image = path_of(root, "图.png")
    plain = {**SMALL, "clips": [{"source": image, "duration": 1}]}
    _, _, without = await run(media, ff, settings, plain, task="j_plain")
    titled = {
        **plain,
        "subtitles": {
            "cues": [{"start": 0.2, "end": 0.8, "text": "Hi 冬天减脂比夏天难吗"}],
            "style": {"y": 0.9, "size": 80},
        },
    }
    _, _, with_titles = await run(media, ff, settings, titled, task="j_titled")
    band = slice(360 * 3 * 560, 360 * 3 * 610)
    assert pixels(without, 0.5, 360, 640)[band] != pixels(with_titles, 0.5, 360, 640)[band]
    # Outside the cue's time the frame is the same as without subtitles.
    assert pixels(without, 0.05, 360, 640)[band] == pixels(with_titles, 0.05, 360, 640)[band]


def test_long_lines_wrap_evenly_at_punctuation():
    assert wrap("冬天减脂比夏天难吗", 16) == ["冬天减脂比夏天难吗"]
    assert wrap("冬天减脂比夏天难吗，其实关键在热量缺口", 16) == [
        "冬天减脂比夏天难吗，",
        "其实关键在热量缺口",
    ]
    assert wrap("每天少吃三百大卡就能看到变化了", 10) == ["每天少吃三百大卡", "就能看到变化了"]
    # Latin letters count half and words are not split.
    assert wrap("keep a calorie deficit every day", 9) == ["keep a calorie", "deficit every day"]


async def test_only_the_changed_shot_is_encoded_again(root, media, ff, settings):
    video = path_of(root, "横屏.mp4")
    clips = [{"source": video, "start": t, "duration": 0.5} for t in (0, 1, 2)]
    await run(media, ff, settings, {**SMALL, "clips": clips}, task="j_one")
    cache = root / "_work" / ".cache"
    first = {p.name for p in cache.iterdir()}
    assert len(first) == 3
    clips[1] = {"source": path_of(root, "图.png"), "duration": 0.5}
    _, reports, _ = await run(media, ff, settings, {**SMALL, "clips": clips}, task="j_two")
    assert len({p.name for p in cache.iterdir()} - first) == 1
