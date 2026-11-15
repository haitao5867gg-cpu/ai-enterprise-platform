import asyncio
import os
import time

from conftest import path_of
from mcp import Client

from aep_media.config import Settings
from aep_media.jobs import RESTARTED, Jobs
from aep_media.paths import Media
from aep_media.server import build

KEY = "a" * 64
OTHER = "b" * 64


def slow(root):
    """A render that takes a while: long shots, full size, best quality."""
    video = path_of(root, "横屏.mp4")
    return {
        "clips": [{"source": video, "fit": "blur"} for _ in range(8)],
        "output": {"name": "慢.mp4", "quality": "high"},
    }


async def call(client, tool, args, key=None):
    meta = {"aep/job_key": key} if key else None
    return await client.call_tool(tool, args, meta=meta)


async def until_done(client, job_id, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        reply = (await call(client, "aep_job_status", {"job_id": job_id})).structured_content
        if reply["aep_job"]["status"] not in ("queued", "running"):
            return reply["aep_job"]
        await asyncio.sleep(0.1)
    raise AssertionError("job did not finish")


async def test_tools_answer_over_mcp(root):
    server, jobs = build(Settings(media_dir=root))
    async with Client(server) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == [
            "probe",
            "frames",
            "render",
            "aep_job_status",
            "aep_job_cancel",
        ]
        assert all(t.annotations.read_only_hint for t in tools.tools)
        probed = await call(client, "probe", {"source": path_of(root, "配音.m4a")})
        assert probed.structured_content["kind"] == "audio"
        bad = await call(client, "probe", {"source": "../x.mp4"})
        assert bad.is_error and "路径不对：../x.mp4" in bad.content[0].text
        # Frames come back at once, as a finished job.
        taken = await call(client, "frames", {"source": path_of(root, "横屏.mp4"), "count": 2})
        job = taken.structured_content["aep_job"]
        assert job["status"] == "done" and len(job["result"]["frames"]) == 2
        assert job["result"]["frames"][0]["image"]["aep_file"]["path"].startswith(
            f"_work/{job['job_id']}/"
        )
        failed = await call(client, "frames", {"source": path_of(root, "配音.m4a")})
        assert failed.is_error and "音频没有画面" in failed.content[0].text
        missing = await call(client, "aep_job_status", {"job_id": "j_nope"})
        assert missing.is_error and "没有这个任务" in missing.content[0].text
    await jobs.close()


async def test_a_render_is_a_long_job_that_a_key_finds_again(root):
    server, jobs = build(Settings(media_dir=root))
    args = {
        "width": 360,
        "height": 640,
        "clips": [{"source": path_of(root, "横屏.mp4"), "duration": 1}],
        "subtitles": {"cues": [{"start": 0, "end": 1, "text": "第一句"}]},
    }
    async with Client(server) as client:
        started = (await call(client, "render", args, KEY)).structured_content["aep_job"]
        assert started["status"] in ("queued", "running") and started["result"] is None
        again = (await call(client, "render", args, KEY)).structured_content["aep_job"]
        assert again["job_id"] == started["job_id"]
        done = await until_done(client, started["job_id"])
        assert done["status"] == "done" and done["progress"] == 1
        video = done["result"]["video"]["aep_file"]
        assert video == {"path": f"_work/{started['job_id']}/成片.mp4", "name": "成片.mp4"}
        assert (root / video["path"]).stat().st_size > 0
        # Asked again after it finished: the same job and result, no second render.
        known = (await call(client, "render", args, KEY)).structured_content["aep_job"]
        assert (known["job_id"], known["status"]) == (started["job_id"], "done")
        other = (await call(client, "render", args, OTHER)).structured_content["aep_job"]
        assert other["job_id"] != started["job_id"]
        await until_done(client, other["job_id"])
        bad = await call(client, "render", {**args, "clips": []}, "c" * 64)
        assert bad.is_error and "clips 要有 1–300 个镜头" in bad.content[0].text
    await jobs.close()


async def test_renders_queue_and_cancel(root):
    server, jobs = build(Settings(media_dir=root, media_max_jobs=1))
    async with Client(server) as client:
        first = (await call(client, "render", slow(root), KEY)).structured_content["aep_job"]
        second = (await call(client, "render", slow(root), OTHER)).structured_content["aep_job"]
        assert second["status"] == "queued"
        assert second["message"] == "排队中，前面还有 1 个"
        await asyncio.sleep(0.5)
        status = await call(client, "aep_job_status", {"job_id": first["job_id"]})
        assert status.structured_content["aep_job"]["status"] == "running"
        assert status.structured_content["aep_job"]["message"].startswith(
            "渲染第 1 个镜头，共 8 个"
        )
        stopped = await call(client, "aep_job_cancel", {"job_id": first["job_id"]})
        assert stopped.structured_content["aep_job"]["status"] == "cancelled"
        assert not (root / "_work" / first["job_id"]).exists()
        # The queued one moves up and is cancelled too.
        await call(client, "aep_job_cancel", {"job_id": second["job_id"]})
        assert jobs.jobs[second["job_id"]].status == "cancelled"
        # A cancelled job's key starts a new one.
        third = (await call(client, "render", slow(root), KEY)).structured_content["aep_job"]
        assert third["job_id"] not in (first["job_id"], second["job_id"])
        await call(client, "aep_job_cancel", {"job_id": third["job_id"]})
    await jobs.close()
    assert not any(name.endswith(".part.mp4") for name in os.listdir(root / "_work" / ".cache"))


async def test_a_restarted_connector_knows_finished_jobs(root):
    server, jobs = build(Settings(media_dir=root))
    args = {"width": 360, "height": 640, "clips": [{"source": path_of(root, "图.png"),
                                                     "duration": 0.5}]}  # fmt: skip
    async with Client(server) as client:
        done = (await call(client, "render", args, KEY)).structured_content["aep_job"]
        done = await until_done(client, done["job_id"])
        going = (await call(client, "render", slow(root), OTHER)).structured_content["aep_job"]
    # A second process over the same volume, while the first still renders.
    restarted = Jobs(Media(root), slots=1, keep_hours=24)
    assert restarted.find(KEY).status == "done"
    assert restarted.get(done["job_id"]).result == done["result"]
    lost = restarted.get(going["job_id"])
    assert (lost.status, lost.error) == ("error", RESTARTED)
    # Its key starts the work again.
    assert restarted.find(OTHER) is None
    await jobs.cancel(going["job_id"])
    await jobs.close()


async def test_old_work_files_and_records_are_swept(root):
    media = Media(root)
    media.prepare()
    jobs = Jobs(media, slots=1, keep_hours=1)
    old = time.time() - 7200
    stale = media.task_dir("t_old")
    (stale / "frame.jpg").write_bytes(b"x")
    fresh = media.task_dir("t_new")
    cached = media.work / ".cache" / "shot_x.mp4"
    cached.write_bytes(b"x")
    record = media.work / ".jobs" / "j_old.json"
    record.write_text('{"job_id": "j_old", "tool": "render", "key": "k", "status": "done",'
                      ' "created": 0}', encoding="utf-8")  # fmt: skip
    for path in (stale, cached, record):
        os.utime(path, (old, old))
    jobs = Jobs(media, slots=1, keep_hours=1)
    assert jobs.find("k") is not None
    jobs.sweep()
    assert not stale.exists() and not cached.exists() and not record.exists()
    assert fresh.exists() and jobs.find("k") is None
    # At most once an hour.
    os.utime(fresh, (old, old))
    jobs.sweep()
    assert fresh.exists()
