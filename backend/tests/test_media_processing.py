"""The platform's side of media processing (docs/api/media-processing.md): uploads probed
by the media processing connector, thumbnails, and tool nodes that save the files they
made into a media library."""

import asyncio
import uuid
from typing import Any

import pytest

from aep.main import app
from aep.media import outputs, probe
from aep.media.files import UNSUPPORTED
from aep.media.outputs import SaveError, import_key, save_outputs
from aep.media.probe import Prober
from aep.media.store import ItemQuery, MediaItem
from aep.runtime.flow.media import summary
from tests import fake_connector
from tests.test_app_media import Media, coach_flow, problems

VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4


class Processing(Media):
    """The media test platform with the media processing connector set (``connector``)
    and a data source for it."""

    def __init__(self, tmp_path: Any, connector: bool = True, **settings: Any) -> None:
        self.connector = fake_connector.MediaConnector(tmp_path / "media")
        server = self.connector.build()
        url = fake_connector.MEDIA_URL if connector else ""
        super().__init__(tmp_path, media_connector_url=url, **settings)
        tools = self.service._ctx.tools
        resolve = tools._resolve
        tools._resolve = lambda u: (
            self.connector.resolve(server) if u == fake_connector.MEDIA_URL else resolve(u)
        )
        self.prober = Prober(self.media, tools, self.settings)

    async def __aenter__(self) -> "Processing":
        await super().__aenter__()
        app.state.media_prober = self.prober
        self.processing = (
            await self.sources.create("媒体处理", "mcp", fake_connector.MEDIA_URL)
        ).datasource_id
        return self

    async def __aexit__(self, *exc: Any) -> None:
        app.state.media_prober = None
        await super().__aexit__(*exc)

    async def upload(self, library_id: str, *names: str) -> list[dict]:
        resp = await self.api.post(
            f"/media-libraries/{library_id}/items",
            files=[("files", (name, VIDEO)) for name in names],
        )
        assert resp.status_code == 202, resp.text
        return resp.json()

    async def item(self, library_id: str, item_id: str) -> dict:
        resp = await self.api.get(f"/media-libraries/{library_id}/items/{item_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def saved(self, library_id: str) -> list[MediaItem]:
        return (await self.media.items(library_id, ItemQuery()))[0]

    def work(self) -> list[str]:
        """The connector's work files still on the disk."""
        work = self.settings.media_dir / "_work"
        return sorted(str(p.relative_to(work)) for p in work.rglob("*") if p.is_file())


# Probing uploads


async def test_uploads_are_probed_and_get_a_thumbnail(tmp_path):
    async with Processing(tmp_path) as p:
        await p.new_app("coach", coach_flow(p.voices, p.footage))
        video, voice, broken = await p.upload(p.footage, "横屏.mp4", "配音.m4a", "坏.mp4")
        assert {i["status"] for i in (video, voice, broken)} == {"processing"}
        assert not video["has_thumbnail"]

        await p.prober.sweep()
        got = await p.item(p.footage, video["item_id"])
        assert (got["status"], got["error"], got["duration_seconds"]) == ("ready", None, 12.5)
        assert (got["width"], got["height"], got["has_thumbnail"]) == (1080, 1920, True)
        heard = await p.item(p.footage, voice["item_id"])
        assert (heard["status"], heard["width"], heard["has_thumbnail"]) == ("ready", None, False)
        failed = await p.item(p.footage, broken["item_id"])
        assert failed["status"] == "error"
        assert failed["error"] == "探测失败：不是有效的视频：没有画面"
        asked = {c["source"].rsplit("/", 1)[-1]: c["thumbnail"] for c in p.connector.probed}
        assert asked == {"横屏.mp4": True, "配音.m4a": False, "坏.mp4": True}

        # The thumbnail moved next to the library's items; nothing is left in _work.
        kept = p.settings.media_dir / p.footage / "_thumbnails" / f"{video['item_id']}.jpg"
        assert kept.read_bytes() == fake_connector.JPEG
        assert p.work() == []
        own = f"/media-libraries/{p.footage}/items/{video['item_id']}/thumbnail"
        shown = await p.api.get(own)
        assert shown.status_code == 200 and shown.content == fake_connector.JPEG
        assert shown.headers["content-type"] == "image/jpeg"
        none = await p.api.get(f"/media-libraries/{p.footage}/items/{voice['item_id']}/thumbnail")
        assert (none.status_code, none.json()["detail"]) == (404, "这个素材没有缩略图")
        missing = await p.api.get(f"/media-libraries/{p.footage}/items/{uuid.uuid4()}/thumbnail")
        assert missing.status_code == 404

        # Pages with the app's key see it too, for libraries the app binds.
        headers = await p.key()
        path = f"/v1/apps/coach/media-libraries/{p.footage}/items/{video['item_id']}"
        assert (await p.api.get(f"{path}/thumbnail", headers=headers)).content == (
            fake_connector.JPEG
        )
        assert (await p.api.get(path, headers=headers)).json()["has_thumbnail"] is True
        other = (await p.media.create_library("没绑定")).library_id
        [loose] = await p.upload(other, "别的.mp4")
        await p.prober.sweep()
        loose_path = f"/v1/apps/coach/media-libraries/{other}/items/{loose['item_id']}/thumbnail"
        assert (await p.api.get(loose_path, headers=headers)).status_code == 404

        # Deleting the item deletes its thumbnail.
        resp = await p.api.delete(f"/media-libraries/{p.footage}/items/{video['item_id']}")
        assert resp.status_code == 204, resp.text
        assert not kept.exists()


async def test_items_wait_while_the_connector_is_away(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "BATCH", 2)
    async with Processing(tmp_path) as p:
        p.connector.down = 1
        [first] = await p.upload(p.footage, "一.mp4")
        await p.prober.sweep()
        assert (await p.item(p.footage, first["item_id"]))["status"] == "processing"

        # Items left processing (as after a restart) are probed in batches, oldest first.
        later = [await p.add(p.footage, f"{n}.mp4", status="processing") for n in "二三四"]
        await p.prober.sweep()
        for item_id in [first["item_id"], *(i.item_id for i in later)]:
            assert (await p.item(p.footage, item_id))["status"] == "ready"
        assert p.connector.probed[0]["source"].endswith("一.mp4")

        # Running in the background, an upload is probed at once rather than a minute on.
        task = asyncio.create_task(p.prober.run())
        try:
            [fresh] = await p.upload(p.footage, "五.mp4")
            for _ in range(100):
                if (await p.item(p.footage, fresh["item_id"]))["status"] == "ready":
                    break
                await asyncio.sleep(0.02)
            assert (await p.item(p.footage, fresh["item_id"]))["status"] == "ready"
        finally:
            task.cancel()


async def test_without_the_connector_uploads_are_ready_at_once(tmp_path):
    async with Processing(tmp_path, connector=False) as p:
        assert not p.prober.enabled
        [item] = await p.upload(p.footage, "横屏.mp4")
        assert (item["status"], item["has_thumbnail"]) == ("ready", False)
        assert p.connector.probed == []


# Saving what a tool made


def cut_flow(source: str, library: str, files: list[dict], access: str = "write", **save) -> dict:
    """``make`` writes ``files`` and its node saves them into ``library``."""
    return {
        "media": {"libraries": [{"library_id": library, "access": access}]},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [{"key": "topic", "label": "主题", "type": "text"}],
                },
                {
                    "id": "make",
                    "type": "tool",
                    "title": "渲染",
                    "datasource": source,
                    "tool": "make",
                    "args": {"files": files},
                    "save_media": {"library": library, **save},
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "made", "from": ["{{make.result}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "make"}, {"from": "make", "to": "end"}],
        },
    }


async def test_a_tool_node_saves_the_files_it_made(tmp_path):
    async with Processing(tmp_path) as p:
        files = [{"name": "成片.mp4"}, {"name": "封面.jpg", "size": 100}]
        config = cut_flow(
            p.processing,
            p.footage,
            files,
            description="{{start.topic}}的成片",
            tags=["成片", " 自动 ", "成片"],
        )
        await p.new_app("cut", config)
        run = await p.run("cut", {"topic": "冬天减脂"})
        assert run["status"] == "done", run
        made = run["result"]["made"]

        items = sorted(await p.saved(p.footage), key=lambda i: i.name, reverse=True)
        assert [i.name for i in items] == ["成片.mp4", "封面.jpg"]
        video, cover = items
        assert made == {"video": summary(video), "all": [summary(video), summary(cover)],
                        "count": 2}  # fmt: skip
        assert video.source == {"kind": "run", "run_id": run["run_id"]}
        assert (video.description, video.tags) == ("冬天减脂的成片", ("成片", "自动"))
        assert (video.status, video.duration_seconds, video.has_thumbnail) == ("ready", 12.5, True)
        assert (video.size_bytes, cover.size_bytes) == (2048, 100)
        assert (p.settings.media_dir / video.rel_path).stat().st_size == 2048
        assert p.work() == []

        # The run's files list them, on the run page and through the app API.
        listed = (await p.api.get(f"/runs/{run['run_id']}/media")).json()
        assert sorted(f["name"] for f in listed) == ["封面.jpg", "成片.mp4"]
        headers = await p.key("cut")
        got = (await p.api.get(f"/v1/runs/{run['run_id']}", headers=headers)).json()
        assert len(got["files"]) == 2


async def test_save_media_is_checked_when_saved(tmp_path):
    async with Processing(tmp_path) as p:
        resp = await p.api.post("/apps", json={"app": "coach", "title": "剪辑"})
        assert resp.status_code == 201, resp.text
        files = [{"name": "成片.mp4"}]
        where = ("graph", "nodes", "make", "save_media")
        read_only = cut_flow(p.processing, p.footage, files, access="read")
        assert await problems(p, read_only) == {
            (*where, "library"): "存进的媒体库要以“写”权限绑定到应用"
        }
        unbound = cut_flow(p.processing, p.footage, files)
        unbound["graph"]["nodes"][1]["save_media"]["library"] = p.voices
        assert await problems(p, unbound) == {
            (*where, "library"): "存进的媒体库要以“写”权限绑定到应用"
        }
        bad = cut_flow(p.processing, p.footage, files, description=3, tags=["x" * 21])
        bad["graph"]["nodes"][1]["save_media"]["library"] = ""
        assert await problems(p, bad) == {
            (*where, "library"): "要选存进哪个媒体库",
            (*where, "description"): "说明要是最多 500 个字符的文字",
            (*where, "tags"): "标签最多 20 个，每个 1–20 个字",
        }
        wrong = cut_flow(p.processing, p.footage, files)
        wrong["graph"]["nodes"][1]["save_media"] = "footage"
        assert await problems(p, wrong) == {where: '要是 {"library", "description", "tags"}'}


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"name": "x.mp4", "path": "LIBRARY_FILE"}, "路径不在连接器的工作目录里"),
        ({"name": "x.mp4", "path": "_work/../etc/passwd"}, "路径不在连接器的工作目录里"),
        ({"name": "x.mp4", "path": "/etc/passwd"}, "路径不在连接器的工作目录里"),
        ({"name": "x.mp4", "link": "LIBRARY_FILE"}, "路径不在连接器的工作目录里"),
        ({"name": "x.mp4", "path": "_work/t_9/none.mp4"}, "文件不见了：_work/t_9/none.mp4"),
        ({"name": "说明.txt"}, UNSUPPORTED.format(file="说明.txt")),
        ({"name": "空.mp4", "size": 0}, "文件「空.mp4」：是空文件"),
        ({"name": "大.mp4", "size": 1024 * 1024 + 1}, "文件「大.mp4」：单个文件不能超过 1 MB"),
    ],
)
async def test_files_that_cannot_be_saved_fail_the_node_and_save_nothing(tmp_path, spec, message):
    async with Processing(tmp_path) as p:
        kept = await p.add(p.voices, "配音.m4a")
        for key in ("path", "link"):
            if spec.get(key) == "LIBRARY_FILE":
                spec = {**spec, key: kept.rel_path}
        files = [{"name": "好的.mp4"}, spec]
        await p.new_app("cut", cut_flow(p.processing, p.footage, files))
        run = await p.run("cut", {})
        assert run["status"] == "error", run
        assert run["error"].startswith("节点「渲染」(make)：存进媒体库失败：")
        assert message in run["error"]
        assert await p.saved(p.footage) == []
        # The good file stays where the tool left it; the library's own file is untouched.
        assert "t_1/好的.mp4" in p.work()
        assert (p.settings.media_dir / kept.rel_path).is_file()


async def test_a_single_node_test_saves_nothing(tmp_path):
    async with Processing(tmp_path) as p:
        await p.new_app("cut", cut_flow(p.processing, p.footage, [{"name": "成片.mp4"}]))
        resp = await p.api.post("/apps/cut/nodes/make/test", json={"values": {}})
        assert resp.status_code == 200, resp.text
        result = resp.json()["outputs"]["result"]
        assert result["video"] == {"aep_file": {"path": "_work/t_1/成片.mp4", "name": "成片.mp4"}}
        assert await p.saved(p.footage) == []


async def test_saving_again_finds_what_this_run_saved(tmp_path):
    async with Processing(tmp_path) as p:
        root = p.settings.media_dir
        work = root / "_work" / "j_1"
        work.mkdir(parents=True)
        (work / "成片.mp4").write_bytes(b"\x00" * 10)
        value = {"video": {"aep_file": {"path": "_work/j_1/成片.mp4", "name": "成片.mp4"}}}

        async def save(run_id: str = "r-1", prober: Prober | None = None, **more: Any) -> Any:
            return await save_outputs(
                value,
                store=p.media,
                settings=p.settings,
                prober=prober,
                library_id=p.footage,
                run_id=run_id,
                summary=summary,
                **more,
            )

        first = await save()
        [item] = await p.saved(p.footage)
        assert (item.status, item.import_key) == ("ready", import_key("r-1", "_work/j_1/成片.mp4"))
        assert first == {"video": summary(item)}
        # Run again (the node re-executed after a restart): the same item, nothing new.
        assert await save() == first
        assert len(await p.saved(p.footage)) == 1
        # Another run whose file is gone cannot save it.
        with pytest.raises(SaveError, match="文件不见了"):
            await save("r-2")
        # No references: the value as it was.
        assert await save_outputs(
            {"n": 1}, store=p.media, settings=p.settings, prober=None, library_id="x",
            run_id="r", summary=summary,
        ) == {"n": 1}  # fmt: skip


async def test_a_lost_database_leaves_the_files_for_a_retry(tmp_path, monkeypatch):
    async with Processing(tmp_path) as p:
        root = p.settings.media_dir
        work = root / "_work" / "j_1"
        work.mkdir(parents=True)
        (work / "成片.mp4").write_bytes(b"\x00" * 10)
        value = {"video": {"aep_file": {"path": "_work/j_1/成片.mp4", "name": "成片.mp4"}}}

        async def save() -> Any:
            return await save_outputs(
                value, store=p.media, settings=p.settings, prober=None,
                library_id=p.footage, run_id="r-1", summary=summary,
            )  # fmt: skip

        real = p.media.add_items

        async def gone(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection to the database was lost")

        monkeypatch.setattr(p.media, "add_items", gone)
        with pytest.raises(RuntimeError, match="database was lost"):
            await save()
        # Nothing registered, and the file is back where the tool left it.
        assert await p.saved(p.footage) == [] and p.work() == ["j_1/成片.mp4"]
        assert [d for d in (root / p.footage).iterdir()] == []

        # The records were written, but the answer was lost: the files stay with them.
        async def written_then_lost(*args: Any, **kwargs: Any) -> Any:
            await real(*args, **kwargs)
            raise RuntimeError("connection to the database was lost")

        monkeypatch.setattr(p.media, "add_items", written_then_lost)
        with pytest.raises(RuntimeError):
            await save()
        [item] = await p.saved(p.footage)
        assert (root / item.rel_path).is_file() and p.work() == []
        # The node run again finds them.
        monkeypatch.setattr(p.media, "add_items", real)
        assert await save() == {"video": summary(item)}


async def test_what_saving_refuses(tmp_path, monkeypatch):
    async with Processing(tmp_path, media_max_items=1) as p:
        root = p.settings.media_dir
        refs = []
        for n in range(2):
            folder = root / "_work" / f"j_{n}"
            folder.mkdir(parents=True)
            (folder / f"{n}.mp4").write_bytes(b"\x00" * 10)
            refs.append({"aep_file": {"path": f"_work/j_{n}/{n}.mp4", "name": f"{n}.mp4"}})

        async def save(value: Any, library_id: str | None = None) -> Any:
            return await save_outputs(
                value,
                store=p.media,
                settings=p.settings,
                prober=p.prober,
                library_id=library_id or p.footage,
                run_id="r-1",
                summary=summary,
            )

        with pytest.raises(SaveError, match="一个媒体库最多 1 个素材"):
            await save(refs)
        with pytest.raises(SaveError, match="没有这个媒体库"):
            await save(refs, str(uuid.uuid4()))
        monkeypatch.setattr(outputs, "MAX_SAVED", 1)
        with pytest.raises(SaveError, match="一次最多存 1 个文件，工具产出了 2 个"):
            await save(refs)
        assert await p.saved(p.footage) == []
        assert p.work() == ["j_0/0.mp4", "j_1/1.mp4"]

        # The connector away while saving: kept as it is, ready to use.
        p.connector.down = 1
        saved = await save(refs[:1])
        [item] = await p.saved(p.footage)
        assert (item.status, item.duration_seconds) == ("ready", None)
        assert saved == [summary(item)]
