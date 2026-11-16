import asyncio
import time

import httpx
from conftest import KEY, work
from mcp import Client

from aep_stock.config import Settings
from aep_stock.pixabay import LICENSE, LICENSE_URL
from aep_stock.server import build

JOB_KEY = "a" * 64


async def call(client, tool, args, key=None):
    meta = {"aep/job_key": key} if key else None
    return await client.call_tool(tool, args, meta=meta)


async def until_done(client, job_id, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        reply = (await call(client, "aep_job_status", {"job_id": job_id})).structured_content
        if reply["aep_job"]["status"] not in ("queued", "running"):
            return reply["aep_job"]
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_the_tools(settings, transport):
    server, jobs, pixabay = build(settings, transport)
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == [
            "search_videos",
            "download_video",
            "aep_job_status",
            "aep_job_cancel",
        ]
        assert all(t.annotations.read_only_hint for t in tools)
        missing = await call(client, "aep_job_status", {"job_id": "j_nope"})
        assert missing.is_error and "没有这个任务" in missing.content[0].text
    await jobs.close()
    await pixabay.close()


async def test_a_search_keeps_sharp_enough_clips_of_the_shape_and_length_asked(
    settings, transport, fake
):
    server, jobs, pixabay = build(settings, transport)
    async with Client(server) as client:

        async def search(**args):
            reply = await call(client, "search_videos", {"query": "cooking", **args})
            assert not reply.is_error, reply.content[0].text
            return reply.structured_content

        found = await search()
        # 720p at best (3) is left out; the smallest version at least 1080 is the one used.
        assert [i["id"] for i in found["items"]] == ["pixabay:1", "pixabay:4"]
        assert found["total"] == 2 and found["site"] == "Pixabay"
        first = found["items"][0]
        assert first == {
            "id": "pixabay:1",
            "site": "Pixabay",
            "url": "https://pixabay.com/videos/id-1/",
            "author": "作者1",
            "tags": ["woman", "cooking", "kitchen"],
            "seconds": 14,
            "width": 1080,
            "height": 1920,
            "orientation": "portrait",
            "thumbnail": None,
        }
        assert found["items"][1]["orientation"] == "square"
        assert [i["id"] for i in (await search(orientation="portrait"))["items"]] == ["pixabay:1"]
        assert (await search(orientation="landscape"))["items"] == []
        assert [i["id"] for i in (await search(min_short_side=720))["items"]] == [
            "pixabay:1",
            "pixabay:3",
            "pixabay:4",
        ]
        assert [i["id"] for i in (await search(min_seconds=6))["items"]] == ["pixabay:1"]
        assert [i["id"] for i in (await search(max_seconds=10))["items"]] == ["pixabay:4"]
        limited = await search(limit=1)
        assert len(limited["items"]) == 1 and limited["total"] == 2
        running = await call(client, "search_videos", {"query": "treadmill"})
        assert running.structured_content["items"][0]["width"] == 1920  # not the 4K one

        # Real footage, safe search, the 100 most popular, in the language asked.
        asked = fake.api_calls[0]
        assert asked["video_type"] == "film" and asked["safesearch"] == "true"
        assert asked["per_page"] == "100" and asked["lang"] == "en"
        await search(lang="zh")
        assert fake.api_calls[-1]["lang"] == "zh"

        bad = await call(client, "search_videos", {"query": "x" * 101})
        assert bad.is_error
        bad = await call(client, "search_videos", {"query": "cooking", "orientation": "round"})
        assert bad.is_error
    await jobs.close()
    await pixabay.close()


async def test_the_same_search_is_asked_once_a_day(settings, transport, fake):
    server, jobs, pixabay = build(settings, transport)
    now = [1000.0]
    pixabay.clock = lambda: now[0]
    async with Client(server) as client:
        for query in ("cooking", "Cooking ", " cooking"):
            reply = await call(client, "search_videos", {"query": query, "limit": 1})
            assert not reply.is_error
        assert len(fake.api_calls) == 1
        now[0] += 23 * 3600
        await call(client, "search_videos", {"query": "cooking", "min_short_side": 720})
        assert len(fake.api_calls) == 1
        now[0] += 2 * 3600
        await call(client, "search_videos", {"query": "cooking"})
        assert len(fake.api_calls) == 2
    await jobs.close()
    await pixabay.close()


async def test_thumbnails_are_work_files_and_a_missing_one_is_null(settings, transport, fake):
    server, jobs, pixabay = build(settings, transport)
    async with Client(server) as client:
        reply = await call(
            client, "search_videos", {"query": "kitchen treadmill", "thumbnails": True}
        )
        items = {i["id"]: i for i in reply.structured_content["items"]}
        assert items["pixabay:2"]["thumbnail"] is None  # its poster could not be had
        ref = items["pixabay:1"]["thumbnail"]["aep_file"]
        assert ref == {"path": "_work/stock/thumbs/pixabay-1.jpg", "name": "pixabay-1.jpg"}
        assert (settings.media_dir / ref["path"]).read_bytes().startswith(b"\xff\xd8")
        # The poster is the smallest one of at least 360 px.
        assert not list(work(settings.media_dir).glob("*.part-*"))
    await jobs.close()
    await pixabay.close()


async def test_a_download_gets_the_smallest_sharp_enough_version_with_its_source(
    settings, transport, fake
):
    server, jobs, pixabay = build(settings, transport)
    async with Client(server) as client:
        reply = await call(client, "download_video", {"id": "pixabay:2"}, JOB_KEY)
        job = reply.structured_content["aep_job"]
        assert job["status"] == "done"
        result = job["result"]
        assert result == {
            "file": {
                "aep_file": {
                    "path": "_work/stock/pixabay-2-1920x1080.mp4",
                    "name": "pixabay-2.mp4",
                    "source": {
                        "kind": "stock",
                        "site": "Pixabay",
                        "url": "https://pixabay.com/videos/id-2/",
                        "author": "作者2",
                        "license": LICENSE,
                        "license_url": LICENSE_URL,
                    },
                }
            },
            "width": 1920,
            "height": 1080,
            "seconds": 30,
        }
        path = settings.media_dir / result["file"]["aep_file"]["path"]
        assert path.stat().st_size == 1_080_000
        assert fake.downloads == ["2-1_medium.mp4"]

        # Asked again under its key: the same job. Again under no key: the file is there.
        again = await call(client, "download_video", {"id": "pixabay:2"}, JOB_KEY)
        assert again.structured_content["aep_job"]["job_id"] == job["job_id"]
        other = await call(client, "download_video", {"id": "pixabay:2"})
        assert other.structured_content["aep_job"]["result"] == result
        assert fake.downloads == ["2-1_medium.mp4"]
        sharper = await call(client, "download_video", {"id": "pixabay:2", "min_short_side": 2160})
        assert sharper.structured_content["aep_job"]["result"]["width"] == 3840

        for args, said in [
            ({"id": "12345"}, "id 要写成 pixabay:数字"),
            ({"id": "pixabay:3"}, "这段视频没有短边 1080 像素以上的版本"),
            ({"id": "pixabay:99"}, "Pixabay 上没有这段视频了：pixabay:99"),
        ]:
            failed = await call(client, "download_video", args)
            assert failed.is_error and said in failed.content[0].text
    await jobs.close()
    await pixabay.close()


async def test_a_file_over_the_limit_is_not_downloaded(tmp_path, transport, fake):
    settings = Settings(media_dir=tmp_path, stock_pixabay_key=KEY, stock_max_mb=1)
    server, jobs, pixabay = build(settings, transport)
    async with Client(server) as client:
        failed = await call(client, "download_video", {"id": "pixabay:1", "min_short_side": 2160})
        assert failed.is_error and "文件超过 1 MB，没有下载" in failed.content[0].text
        assert fake.downloads == []
    await jobs.close()
    await pixabay.close()


async def test_a_slow_download_is_handed_back_and_can_be_cancelled(tmp_path, transport, fake):
    settings = Settings(media_dir=tmp_path, stock_pixabay_key=KEY, stock_wait_seconds=0.3)
    server, jobs, pixabay = build(settings, transport)
    async with Client(server) as client:
        reply = await call(client, "download_video", {"id": "pixabay:5"}, JOB_KEY)
        job = reply.structured_content["aep_job"]
        assert job["status"] == "running" and 0 < job["progress"] < 1
        assert job["message"].startswith("已下载")
        again = await call(client, "download_video", {"id": "pixabay:5"}, JOB_KEY)
        assert again.structured_content["aep_job"]["job_id"] == job["job_id"]
        fake.release.set()
        done = await until_done(client, job["job_id"])
        assert done["status"] == "done" and done["progress"] == 1
        assert (tmp_path / done["result"]["file"]["aep_file"]["path"]).stat().st_size == 1_080_000

        # Cancelled half way: the part file goes.
        (tmp_path / done["result"]["file"]["aep_file"]["path"]).unlink()
        fake.release.clear()
        started = await call(client, "download_video", {"id": "pixabay:5"}, "b" * 64)
        job_id = started.structured_content["aep_job"]["job_id"]
        assert list(work(tmp_path).glob("*.part-*"))
        stopped = await call(client, "aep_job_cancel", {"job_id": job_id})
        assert stopped.structured_content["aep_job"]["status"] == "cancelled"
        assert not list(work(tmp_path).glob("*.part-*"))
        assert not list(work(tmp_path).glob("*.mp4"))
    await jobs.close()
    await pixabay.close()


async def test_what_goes_wrong_is_said_without_the_key(tmp_path, transport, fake):
    async def ask(settings, query="cooking"):
        server, jobs, pixabay = build(settings, transport)
        async with Client(server) as client:
            reply = await call(client, "search_videos", {"query": query})
        await jobs.close()
        await pixabay.close()
        assert reply.is_error
        assert KEY not in reply.content[0].text
        return reply.content[0].text

    good = Settings(media_dir=tmp_path, stock_pixabay_key=KEY)
    assert "没有配置 Pixabay 的 API key（AEP_STOCK_PIXABAY_KEY）" in await ask(
        Settings(media_dir=tmp_path)
    )
    assert fake.api_calls == []  # nothing is asked without a key
    assert "Pixabay 不认这个 API key" in await ask(
        Settings(media_dir=tmp_path, stock_pixabay_key="wrong")
    )
    assert "Pixabay 请求太频繁，过一分钟再试" in await ask(good, "busy")
    assert "连不上 Pixabay：Pixabay 回答 503" in await ask(good, "down")
    assert "Pixabay 的回答看不懂" in await ask(good, "broken")
    fake.fail = httpx.ConnectError(f"cannot reach https://pixabay.com/api/videos/?key={KEY}")
    said = await ask(good, "cooking")
    assert "连不上 Pixabay：cannot reach" in said and "***" in said
