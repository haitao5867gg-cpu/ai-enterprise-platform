import asyncio
import os

import pytest
from conftest import LIB, path_of

from aep_media import frames as frames_module
from aep_media.frames import frames
from aep_media.paths import MediaError
from aep_media.probe import jpeg_size, probe


async def test_probe_reads_kind_size_and_duration(root, media, ff):
    video = await probe(media, ff, path_of(root, "横屏.mp4"), False)
    assert video == {
        "kind": "video",
        "duration_seconds": pytest.approx(4.0, abs=0.1),
        "width": 640,
        "height": 360,
        "orientation": "landscape",
        "fps": 30.0,
        "has_video": True,
        "has_audio": True,
        "video_codec": "h264",
        "audio_codec": "aac",
        "size_bytes": (root / path_of(root, "横屏.mp4")).stat().st_size,
        "thumbnail": None,
    }
    # Filmed upright: the rotation mark turns it the way it is shown.
    upright = await probe(media, ff, path_of(root, "竖拍.mp4"), False)
    assert (upright["width"], upright["height"], upright["orientation"]) == (360, 640, "portrait")
    silent = await probe(media, ff, path_of(root, "无声.mp4"), False)
    assert (silent["has_audio"], silent["audio_codec"]) == (False, None)
    audio = await probe(media, ff, path_of(root, "配音.m4a"), True)
    assert audio["kind"] == "audio"
    assert audio["duration_seconds"] == pytest.approx(6.0, abs=0.1)
    assert (audio["width"], audio["has_video"], audio["thumbnail"]) == (None, False, None)
    image = await probe(media, ff, path_of(root, "图.png"), False)
    assert (image["kind"], image["width"], image["height"], image["duration_seconds"]) == (
        "image",
        800,
        600,
        None,
    )


async def test_probe_makes_a_thumbnail_as_a_work_file(root, media, ff):
    found = await probe(media, ff, {"path": path_of(root, "竖拍.mp4")}, True)
    ref = found["thumbnail"]["aep_file"]
    assert ref["path"].startswith("_work/t_") and ref["name"] == "thumbnail.jpg"
    assert jpeg_size(root / ref["path"]) == (270, 480)
    # A file reference from an earlier tool is a source too.
    again = await probe(media, ff, found["thumbnail"], False)
    assert (again["kind"], again["width"], again["height"]) == ("image", 270, 480)


@pytest.mark.parametrize(
    ("source", "error"),
    [
        (f"{LIB}/item0/坏.mp4", "不是有效的视频"),
        (f"{LIB}/nope/x.mp4", "文件不存在"),
        ("../etc/passwd", "路径不对"),
        ("/etc/passwd", "路径不对"),
        (f"{LIB}/./item0/坏.mp4", "路径不对"),
        (f"{LIB}//item0/坏.mp4", "路径不对"),
        (f"{LIB}\\item0", "路径不对"),
        (LIB, "文件不存在"),
        ("", "素材要写媒体路径"),
        ({"name": "x"}, "素材要写媒体路径"),
    ],
)
async def test_probe_refuses_what_it_cannot_use(root, media, ff, source, error):
    (root / LIB / "item0").mkdir(parents=True, exist_ok=True)
    (root / LIB / "item0" / "坏.mp4").write_bytes(b"not a video")
    with pytest.raises(MediaError, match=error):
        await probe(media, ff, source, False)


async def test_links_out_of_the_volume_are_refused(root, media, ff, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.mp4"
    outside.write_bytes(b"x")
    os.symlink(outside, root / LIB / "link.mp4")
    os.symlink(root / LIB, root / "alias")
    for source in (f"{LIB}/link.mp4", f"alias/{path_of(root, '横屏.mp4').split('/', 1)[1]}"):
        with pytest.raises(MediaError, match="路径不对"):
            await probe(media, ff, source, False)


async def test_frames_at_times_every_and_evenly(root, media, ff):
    video = path_of(root, "横屏.mp4")
    found = await frames(
        media, ff, video, at=[3.5, 1, 9], every_seconds=None, count=6, max_frames=30, width=320
    )
    assert found["duration_seconds"] == pytest.approx(4.0, abs=0.1)
    assert [f["time"] for f in found["frames"]] == [1.0, 3.5]
    first = found["frames"][0]
    assert (first["width"], first["height"]) == (320, 180)
    assert jpeg_size(root / first["image"]["aep_file"]["path"]) == (320, 180)
    every = await frames(
        media, ff, video, at=None, every_seconds=1, count=6, max_frames=30, width=128
    )
    assert [f["time"] for f in every["frames"]] == [0.0, 1.0, 2.0, 3.0]
    even = await frames(media, ff, video, at=None, every_seconds=None, count=4, max_frames=3,
                        width=128)  # fmt: skip
    assert [f["time"] for f in even["frames"]] == [0.5, 1.5, 2.5]
    image = await frames(media, ff, path_of(root, "图.png"), at=None, every_seconds=None,
                         count=6, max_frames=30, width=200)  # fmt: skip
    assert [(f["time"], f["width"], f["height"]) for f in image["frames"]] == [(0.0, 200, 150)]
    with pytest.raises(MediaError, match="音频没有画面"):
        await frames(media, ff, path_of(root, "配音.m4a"), at=None, every_seconds=None,
                     count=6, max_frames=30, width=200)  # fmt: skip


async def test_one_failed_frame_stops_the_others(root, media, ff, monkeypatch):
    started, stopped = [], []

    async def snapshot(ff, path, at, out, *, width=None):
        started.append(at)
        if at == 0:
            raise MediaError("取不出第 0 秒的画面")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.append(at)
            raise
        return 1, 1

    monkeypatch.setattr(frames_module, "snapshot", snapshot)
    with pytest.raises(MediaError, match="取不出第 0 秒的画面"):
        await frames(
            media, ff, path_of(root, "横屏.mp4"),
            at=[0, 1, 2, 3, 3.5], every_seconds=None, count=6, max_frames=30, width=320,
            task="j_x",
        )  # fmt: skip
    # Every frame that started beside it was stopped before the error came back.
    assert 1 in stopped and sorted(stopped) == sorted(t for t in started if t != 0)
