import shutil
import subprocess
from pathlib import Path

import pytest

from aep_media.config import Settings
from aep_media.ffmpeg import FFmpeg
from aep_media.paths import Media

LIB = "11111111-1111-4111-8111-111111111111"


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> Path:
    """A few seconds of test video, audio and images, made once by ffmpeg."""
    root = tmp_path_factory.mktemp("samples")
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4", *tone,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(root / "横屏.mp4"),
    )  # fmt: skip
    # The same picture, filmed upright on a phone: stored landscape with a rotation mark.
    ffmpeg("-display_rotation", "90", "-i", str(root / "横屏.mp4"), "-c", "copy",
           str(root / "竖拍.mp4"))  # fmt: skip
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "无声.mp4"),
    )  # fmt: skip
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=300:duration=6", "-c:a", "aac",
           str(root / "配音.m4a"))  # fmt: skip
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=500:duration=1", str(root / "鼓点.wav"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=800x600:rate=1", "-frames:v", "1",
           str(root / "图.png"))  # fmt: skip
    (root / "坏.mp4").write_bytes(b"not a video at all")
    return root


@pytest.fixture
def root(tmp_path, samples) -> Path:
    """A media volume with the samples in one library, each in its own item folder."""
    for index, sample in enumerate(sorted(samples.iterdir())):
        folder = tmp_path / LIB / f"item{index}"
        folder.mkdir(parents=True)
        shutil.copy(sample, folder / sample.name)
    return tmp_path


def path_of(root: Path, name: str) -> str:
    found = next(root.glob(f"{LIB}/*/{name}"))
    return found.relative_to(root).as_posix()


@pytest.fixture
def settings(root) -> Settings:
    return Settings(media_dir=root)


@pytest.fixture
def media(root) -> Media:
    media = Media(root)
    media.prepare()
    return media


@pytest.fixture
def ff(settings) -> FFmpeg:
    return FFmpeg(settings)
