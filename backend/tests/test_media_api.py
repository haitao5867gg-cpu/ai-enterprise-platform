"""Media library endpoints against the in-memory store and a temporary media directory."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from aep.auth import required
from aep.config import Settings, get_settings
from aep.main import create_app
from aep.media.store import MemoryMediaStore

VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4


@dataclass
class MediaAPI:
    client: httpx.AsyncClient
    store: MemoryMediaStore
    settings: Settings

    async def create(self, name: str = "教练素材", **values: Any) -> dict:
        response = await self.client.post("/media-libraries", json={"name": name, **values})
        assert response.status_code == 201, response.text
        return response.json()

    async def upload(
        self, library_id: str, files: list[tuple[str, bytes]], **form: str
    ) -> httpx.Response:
        return await self.client.post(
            f"/media-libraries/{library_id}/items",
            files=[("files", (name, content)) for name, content in files],
            data=form,
        )

    def stored(self) -> list[Path]:
        root = self.settings.media_dir
        return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


@pytest.fixture
async def api(tmp_path):
    app = create_app()
    settings = Settings(media_dir=tmp_path / "media", media_max_file_mb=1, media_max_items=6)
    store = MemoryMediaStore()
    app.state.media = store
    app.dependency_overrides[get_settings] = lambda: settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield MediaAPI(client, store, settings)


def test_writes_need_a_builder() -> None:
    assert required("POST", "/media-libraries") == "builder"
    assert required("PATCH", "/media-libraries/{library_id}/items/{item_id}") == "builder"
    assert required("POST", "/media-libraries/{library_id}/items") == "builder"
    assert required("GET", "/media-libraries/{library_id}/items/{item_id}/file") == "user"


async def test_library_crud(api):
    library = await api.create(description="王教练自己拍的")
    assert library["name"] == "教练素材" and library["item_count"] == 0
    assert library["status_counts"] == {"processing": 0, "ready": 0, "error": 0}
    lid = library["library_id"]

    response = await api.client.post("/media-libraries", json={"name": " 教练素材 "})
    assert response.status_code == 409
    assert response.json()["detail"] == "已存在名为「教练素材」的媒体库"
    response = await api.client.post("/media-libraries", json={"name": "x" * 51})
    assert response.status_code == 422 and "名称" in response.json()["detail"]
    response = await api.client.post("/media-libraries", json={"name": "a", "description": None})
    assert response.json()["detail"] == "请求字段不能为 null"

    await api.create("网络素材")
    names = [lib["name"] for lib in (await api.client.get("/media-libraries")).json()]
    assert names == sorted(names) and len(names) == 2

    response = await api.client.patch(f"/media-libraries/{lid}", json={"name": "网络素材"})
    assert response.status_code == 409
    response = await api.client.patch(f"/media-libraries/{lid}", json={"description": "开头和动作"})
    assert response.json()["description"] == "开头和动作"
    assert response.json()["name"] == "教练素材"

    assert (await api.client.get(f"/media-libraries/{lid}")).json()["name"] == "教练素材"
    missing = await api.client.get("/media-libraries/nope")
    assert (missing.status_code, missing.json()["detail"]) == (404, "没有这个媒体库")
    english = await api.client.get("/media-libraries/nope", headers={"Accept-Language": "en"})
    assert english.json()["detail"] == "Media library not found"
    assert (await api.client.patch("/media-libraries/nope", json={})).status_code == 404


async def test_upload_list_and_download(api):
    lid = (await api.create())["library_id"]
    response = await api.upload(
        lid,
        [("卧推侧面.MP4", VIDEO), ("../../etc/配音.m4a", b"audio"), ("封面.jpg", b"jpeg")],
        description=" 健身房 ",
        tags=json.dumps(["胸", "卧推", "胸"]),
        uploaded_by="王教练",
    )
    assert response.status_code == 202, response.text
    items = response.json()
    assert [(i["name"], i["kind"], i["size_bytes"]) for i in items] == [
        ("卧推侧面.MP4", "video", len(VIDEO)),
        ("配音.m4a", "audio", 5),
        ("封面.jpg", "image", 4),
    ]
    first = items[0]
    assert first["status"] == "ready" and first["error"] is None
    assert first["duration_seconds"] is None and first["orientation"] is None
    assert first["description"] == "健身房" and first["tags"] == ["胸", "卧推"]
    assert first["usable"] is True and first["uploaded_by"] == "王教练"
    assert first["source"] == {
        "kind": "upload",
        "site": None,
        "url": None,
        "author": None,
        "license": None,
        "license_url": None,
        "run_id": None,
    }
    # Each file at <library_id>/<item_id>/<name>, nothing outside the media directory.
    root = api.settings.media_dir
    assert api.stored() == sorted(root / lid / i["item_id"] / i["name"] for i in items)

    library = (await api.client.get(f"/media-libraries/{lid}")).json()
    assert library["item_count"] == 3 and library["total_bytes"] == len(VIDEO) + 9
    assert library["status_counts"]["ready"] == 3

    page = (await api.client.get(f"/media-libraries/{lid}/items")).json()
    assert page["total"] == 3 and len(page["items"]) == 3
    page = (await api.client.get(f"/media-libraries/{lid}/items?kind=audio")).json()
    assert [i["name"] for i in page["items"]] == ["配音.m4a"]
    page = (await api.client.get(f"/media-libraries/{lid}/items", params={"q": "卧推 mp4"})).json()
    assert [i["name"] for i in page["items"]] == ["卧推侧面.MP4"]
    page = (await api.client.get(f"/media-libraries/{lid}/items?limit=1&offset=1")).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    response = await api.client.get(f"/media-libraries/{lid}/items?limit=0")
    assert response.status_code == 422 and "每页条数" in response.json()["detail"]
    response = await api.client.get(f"/media-libraries/{lid}/items?kind=gif")
    assert response.status_code == 422
    assert (await api.client.get("/media-libraries/nope/items")).status_code == 404

    url = f"/media-libraries/{lid}/items/{first['item_id']}/file"
    whole = await api.client.get(url)
    assert whole.status_code == 200 and whole.content == VIDEO
    assert whole.headers["content-type"] == "video/mp4"
    assert whole.headers["content-disposition"] == (
        "inline; filename*=UTF-8''%E5%8D%A7%E6%8E%A8%E4%BE%A7%E9%9D%A2.MP4"
    )
    part = await api.client.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == VIDEO[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(VIDEO)}"

    (root / lid / first["item_id"] / first["name"]).unlink()
    gone = await api.client.get(url)
    assert (gone.status_code, gone.json()["detail"]) == (404, "素材文件不见了")
    gone = await api.client.get(f"/media-libraries/{lid}/items/{lid}/file")
    assert (gone.status_code, gone.json()["detail"]) == (404, "没有这个素材")


@pytest.mark.parametrize(
    ("files", "form", "detail"),
    [
        ([], {}, "请选择至少一个视频、音频或图片文件"),
        (
            [("ok.mp4", VIDEO), ("说明.pdf", b"pdf")],
            {},
            "文件「说明.pdf」：只支持视频（mp4、mov、m4v、webm、mkv）、"
            "音频（m4a、mp3、wav、aac、ogg、opus、flac）和图片（jpg、jpeg、png、webp）",
        ),
        ([("ok.mp4", VIDEO), ("空.mov", b"")], {}, "文件「空.mov」：是空文件"),
        (
            [("ok.mp4", VIDEO), ("大.mp4", b"0" * (1024 * 1024 + 1))],
            {},
            "文件「大.mp4」：单个文件不能超过 1 MB",
        ),
        ([(f"{i}.mp4", VIDEO) for i in range(21)], {}, "单次最多上传 20 个文件"),
        ([("ok.mp4", VIDEO)], {"tags": "胸"}, "「标签」要是 JSON 数组，最多 20 个，每个 1–20 个字"),
        (
            [("ok.mp4", VIDEO)],
            {"tags": json.dumps(["x" * 21])},
            "「标签」要是 JSON 数组，最多 20 个，每个 1–20 个字",
        ),
        ([("ok.mp4", VIDEO)], {"uploaded_by": "x" * 51}, "「上传人」的格式、长度或范围不正确"),
    ],
)
async def test_a_bad_batch_keeps_nothing(api, files, form, detail):
    lid = (await api.create())["library_id"]
    response = await api.upload(lid, files, **form)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == detail
    assert api.stored() == []
    assert (await api.client.get(f"/media-libraries/{lid}")).json()["item_count"] == 0


async def test_item_limit_and_long_names(api):
    lid = (await api.create())["library_id"]
    long = "卧" * 100 + ".mp4"
    response = await api.upload(lid, [(long, VIDEO)] + [(f"{i}.mp4", VIDEO) for i in range(4)])
    assert response.status_code == 202
    name = response.json()[0]["name"]
    assert name.endswith(".mp4") and len(name.encode()) <= 200 and name.startswith("卧")

    response = await api.upload(lid, [("a.mp4", VIDEO), ("b.mp4", VIDEO)])
    assert (response.status_code, response.json()["detail"]) == (422, "一个媒体库最多 6 个素材")
    assert len(api.stored()) == 5
    english = await api.client.post(
        f"/media-libraries/{lid}/items",
        files=[("files", ("a.mp4", VIDEO)), ("files", ("b.mp4", VIDEO))],
        headers={"Accept-Language": "en"},
    )
    assert english.json()["detail"] == "A media library holds at most 6 items"
    assert (await api.upload("nope", [("a.mp4", VIDEO)])).status_code == 404


async def test_edit_and_delete_items(api):
    lid = (await api.create())["library_id"]
    (item,) = (await api.upload(lid, [("卧推.mp4", VIDEO)])).json()
    url = f"/media-libraries/{lid}/items/{item['item_id']}"

    source = {
        "kind": "stock",
        "site": "Pexels",
        "url": "https://www.pexels.com/video/1/",
        "author": "someone",
        "license": "Pexels License",
        "license_url": "https://www.pexels.com/license/",
    }
    response = await api.client.patch(
        url,
        json={
            "description": "侧面全景",
            "tags": [" 胸 ", "卧推", "胸"],
            "usable": False,
            "source": source,
        },
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["tags"] == ["胸", "卧推"] and changed["usable"] is False
    assert changed["source"] == {**source, "run_id": None}
    assert (await api.client.get(url)).json() == changed
    page = (await api.client.get(f"/media-libraries/{lid}/items?usable=false")).json()
    assert page["total"] == 1

    # The source is replaced whole.
    response = await api.client.patch(url, json={"source": {"kind": "upload"}})
    assert response.json()["source"]["site"] is None
    assert response.json()["description"] == "侧面全景"

    for body, detail in [
        (
            {"source": {"kind": "stock", "url": "javascript:alert(1)"}},
            "来源链接要以 http:// 或 https:// 开头",
        ),
        ({"usable": None}, "请求字段不能为 null"),
        ({"tags": ["x"] * 21}, "「标签」的格式、长度或范围不正确"),
        ({"source": {"kind": "stock", "extra": 1}}, "参数「source.extra」的格式、长度或范围不正确"),
    ]:
        response = await api.client.patch(url, json=body)
        assert (response.status_code, response.json()["detail"]) == (422, detail), body

    other = (await api.create("网络素材"))["library_id"]
    assert (
        await api.client.get(f"/media-libraries/{other}/items/{item['item_id']}")
    ).status_code == 404

    assert (await api.client.delete(url)).status_code == 204
    assert api.stored() == [] and not (api.settings.media_dir / lid / item["item_id"]).exists()
    assert (await api.client.delete(url)).status_code == 404


async def test_delete_library_removes_its_files(api):
    lid = (await api.create())["library_id"]
    keep = (await api.create("网络素材"))["library_id"]
    await api.upload(lid, [("a.mp4", VIDEO), ("b.jpg", b"jpeg")])
    await api.upload(keep, [("c.mp4", VIDEO)])

    assert (await api.client.delete(f"/media-libraries/{lid}")).status_code == 204
    assert not (api.settings.media_dir / lid).exists()
    assert [p.name for p in api.stored()] == ["c.mp4"]
    assert (await api.client.get(f"/media-libraries/{lid}")).status_code == 404
    assert (await api.client.delete(f"/media-libraries/{lid}")).status_code == 404
