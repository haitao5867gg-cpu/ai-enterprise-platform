"""Finding footage by what it shows (docs/api/media-search.md): libraries that have a vision
model describe their videos and images, and the media_search node lists a bound library's
usable items with their descriptions."""

import base64
from typing import Any

import httpx
import pytest

from aep import llm
from aep.main import app
from aep.media.describe import SYSTEM, Describer, clean
from tests import fake_connector
from tests.test_app_media import Media
from tests.test_media_processing import Processing
from tests.test_vision import chats

VISION = {"model": "company-vision", "hint": "健身教练自己拍的素材"}


class Models:
    """The gateway's models as the media API checks the describing one."""

    async def catalog(self) -> list[dict[str, Any]]:
        return [
            {"alias": "company-vision", "vision": True},
            {"alias": "company", "vision": False},
        ]


class Describing(Processing):
    """The media processing platform with a describer and the gateway's models."""

    def __init__(self, tmp_path: Any, **settings: Any) -> None:
        super().__init__(tmp_path, **settings)
        tools = self.service._ctx.tools
        # The gateway as the describer reaches it; tests may put another in between.
        self.handle = self.gateway._handle
        transport = httpx.MockTransport(lambda request: self.handle(request))
        client = llm.make_client(self.settings, http_client=httpx.AsyncClient(transport=transport))
        self.describer = Describer(self.media, tools, self.settings, client)

    async def __aenter__(self) -> "Describing":
        await super().__aenter__()
        self._models = getattr(app.state, "models", None)
        app.state.models = Models()
        app.state.media_describer = self.describer
        return self

    async def __aexit__(self, *exc: Any) -> None:
        app.state.media_describer = None
        app.state.models = self._models
        await super().__aexit__(*exc)

    async def library(self, **describe: Any) -> str:
        resp = await self.api.post(
            "/media-libraries", json={"name": "会描述的库", "describe": {**VISION, **describe}}
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["library_id"]

    async def ready(self, library_id: str, *names: str, **form: Any) -> list[dict]:
        """Upload and probe."""
        resp = await self.api.post(
            f"/media-libraries/{library_id}/items",
            files=[("files", (name, fake_connector.JPEG)) for name in names],
            data=form,
        )
        assert resp.status_code == 202, resp.text
        await self.prober.sweep()
        return [await self.item(library_id, i["item_id"]) for i in resp.json()]


def images(message: dict) -> list[str]:
    return [p["image_url"]["url"] for p in message["content"] if p["type"] == "image_url"]


# Describing


async def test_videos_and_images_of_a_describing_library_are_described(tmp_path):
    async with Describing(tmp_path) as p:
        library = await p.library()
        video, photo, voice = await p.ready(library, "卧推.mp4", "饭.jpg", "配音.m4a")
        [written] = await p.ready(library, "写好的.mp4", description="教练写的")
        p.gateway.replies = ["“健身房杠铃卧推，\n侧面全景，不露脸”", "一盘鸡胸肉和西兰花，俯拍特写"]
        await p.describer.sweep()

        got = await p.item(library, video["item_id"])
        assert got["description"] == "健身房杠铃卧推， 侧面全景，不露脸"
        assert (got["description_by"], got["describe_error"]) == ("model", None)
        seen = await p.item(library, photo["item_id"])
        assert (seen["description"], seen["description_by"]) == (
            "一盘鸡胸肉和西兰花，俯拍特写",
            "model",
        )
        # Audio and what a person wrote are left alone.
        heard = await p.item(library, voice["item_id"])
        assert (heard["description"], heard["description_by"]) == ("", None)
        kept = await p.item(library, written["item_id"])
        assert (kept["description"], kept["description_by"]) == ("教练写的", "person")

        # Four frames of a video, the image itself, 512 wide; then the model saw them.
        asked = {f["source"].rsplit("/", 1)[-1]: f for f in p.connector.framed}
        assert set(asked) == {"卧推.mp4", "饭.jpg"}
        assert (asked["卧推.mp4"]["count"], asked["卧推.mp4"]["width"]) == (4, 512)
        assert asked["饭.jpg"]["count"] is None
        first, second = chats(p)
        assert first["model"] == "company-vision"
        assert first["messages"][0] == {"role": "system", "content": SYSTEM}
        user = first["messages"][1]
        assert user["content"][0]["text"] == (
            "素材库的说明：健身教练自己拍的素材\n文件名：卧推.mp4\n"
            "这是一段12.5 秒的竖屏视频，下面是按时间均匀取的几帧。"
        )
        jpeg = base64.b64encode(fake_connector.JPEG + bytes([0])).decode()
        assert images(user)[0] == f"data:image/jpeg;base64,{jpeg}"
        assert len(images(user)) == 4
        assert "这是一张竖屏图片。" in second["messages"][1]["content"][0]["text"]
        assert len(images(second["messages"][1])) == 1

        # Nothing is left to describe: another round asks nothing.
        await p.describer.sweep()
        assert len(chats(p)) == 2


async def test_a_person_s_description_is_never_written_over(tmp_path):
    async with Describing(tmp_path) as p:
        library = await p.library()
        [cleared] = await p.ready(library, "清空.mp4")
        p.gateway.replies = ["模型写的"]
        await p.describer.sweep()
        # Emptied by a person: not for the model again.
        resp = await p.api.patch(
            f"/media-libraries/{library}/items/{cleared['item_id']}", json={"description": ""}
        )
        assert (resp.json()["description"], resp.json()["description_by"]) == ("", "person")
        await p.describer.sweep()
        assert len(chats(p)) == 1
        # A person writes while the model is describing: theirs is kept.
        [raced] = await p.ready(library, "抢先.mp4")
        [(waiting, describe)] = await p.media.undescribed(10)
        assert waiting.item_id == raced["item_id"]
        await p.media.update_item(
            library, raced["item_id"], description="人写的", description_by="person"
        )
        await p.describer.describe(waiting, describe)
        got = await p.item(library, raced["item_id"])
        assert (got["description"], got["description_by"]) == ("人写的", "person")
        assert len(chats(p)) == 2
        assert (await p.item(library, cleared["item_id"]))["description"] == ""


async def test_what_cannot_be_described_says_why_and_can_be_written_again(tmp_path):
    async with Describing(tmp_path) as p:
        library = await p.library()
        broken, failing, empty = await p.ready(library, "无帧.mp4", "报错.mp4", "空.mp4")
        p.gateway.replies = ["  ", "  "]
        # 报错.mp4 is described first (by name; 无帧.mp4 takes no model): the gateway fails it.
        failures = iter([400, 200])

        def flaky(request: httpx.Request) -> httpx.Response:
            p.gateway.status = next(failures)
            return p.gateway._handle(request)

        p.handle = flaky
        await p.describer.sweep()
        p.handle = p.gateway._handle
        p.gateway.status = 200
        assert (await p.item(library, broken["item_id"]))["describe_error"] == (
            "写描述失败：抽帧失败：抽不出帧：文件坏了"
        )
        assert (await p.item(library, failing["item_id"]))["describe_error"].startswith(
            "写描述失败：模型报错："
        )
        assert (await p.item(library, empty["item_id"]))["describe_error"] == (
            "写描述失败：模型的回答是空的"
        )
        # Not tried again by itself.
        calls = len(chats(p))
        await p.describer.sweep()
        assert len(chats(p)) == calls

        # 「AI 重写」 clears it, and it is described again.
        p.gateway.replies = ["健身房里做卷腹，侧面"]
        path = f"/media-libraries/{library}/items/{empty['item_id']}"
        again = await p.api.post(f"{path}/describe")
        assert again.status_code == 202, again.text
        assert (again.json()["description"], again.json()["describe_error"]) == ("", None)
        await p.describer.sweep()
        got = await p.item(library, empty["item_id"])
        assert (got["description"], got["description_by"]) == ("健身房里做卷腹，侧面", "model")
        # Sending the description unchanged (with new tags) leaves it the model's.
        same = await p.api.patch(path, json={"description": "健身房里做卷腹，侧面", "tags": ["腹"]})
        assert same.json()["description_by"] == "model"
        # A person's description is written over when they ask for it.
        resp = await p.api.patch(path, json={"description": "我自己写的"})
        assert resp.json()["description_by"] == "person"
        p.gateway.replies = ["卷腹，侧面全景"]
        await p.api.post(f"{path}/describe")
        await p.describer.sweep()
        assert (await p.item(library, empty["item_id"]))["description"] == "卷腹，侧面全景"

        # Only in a describing library, only videos and images.
        [voice] = await p.ready(library, "配音.m4a")
        audio = await p.api.post(f"/media-libraries/{library}/items/{voice['item_id']}/describe")
        assert (audio.status_code, audio.json()["detail"]) == (422, "只能给视频和图片写描述")
        [plain] = await p.ready(p.footage, "普通.mp4")
        off = await p.api.post(f"/media-libraries/{p.footage}/items/{plain['item_id']}/describe")
        assert (off.status_code, off.json()["detail"]) == (409, "这个媒体库没有打开自动描述")


async def test_items_wait_while_the_connector_or_the_gateway_is_away(tmp_path):
    async with Describing(tmp_path) as p:
        library = await p.library()
        [video] = await p.ready(library, "卧推.mp4")
        p.connector.down = 1
        await p.describer.sweep()
        assert (await p.item(library, video["item_id"]))["describe_error"] is None
        assert chats(p) == []

        def away(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("gateway restarting")

        p.handle = away
        await p.describer.sweep()
        p.handle = p.gateway._handle
        waiting = await p.item(library, video["item_id"])
        assert (waiting["description"], waiting["describe_error"]) == ("", None)
        p.gateway.replies = ["杠铃卧推"]
        await p.describer.sweep()
        assert (await p.item(library, video["item_id"]))["description"] == "杠铃卧推"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"data_media": "audio"}, "写描述失败：平台不允许把图片发给模型"),
        (
            {"allowed_models": "company"},
            "写描述失败：平台只允许用模型 company，自动描述用的是 company-vision",
        ),
    ],
)
async def test_the_platform_policy_can_keep_frames_in(tmp_path, settings, message):
    async with Describing(tmp_path, **settings) as p:
        library = await p.library()
        [video] = await p.ready(library, "卧推.mp4")
        await p.describer.sweep()
        assert (await p.item(library, video["item_id"]))["describe_error"] == message
        assert chats(p) == [] and p.connector.framed == []


async def test_a_library_s_describing_model_is_checked(tmp_path):
    async with Describing(tmp_path) as p:

        async def made(describe: Any) -> Any:
            return await p.api.post("/media-libraries", json={"name": "新库", "describe": describe})

        missing = await made({"model": "nope"})
        assert (missing.status_code, missing.json()["detail"]) == (422, "网关上没有模型「nope」")
        blind = await made({"model": "company"})
        assert blind.json()["detail"] == ("模型「company」没标能看图，换一个能看图的模型")
        long_hint = await made({"model": "company-vision", "hint": "长" * 201})
        assert long_hint.status_code == 422
        # null is no automatic descriptions, as when left out; other fields cannot be null.
        plain = await made(None)
        assert plain.status_code == 201 and plain.json()["describe"] is None
        null_text = await p.api.post(
            "/media-libraries", json={"name": "另一个", "description": None}
        )
        assert null_text.status_code == 422
        library = await p.library(hint="")
        path = f"/media-libraries/{library}"
        assert (await p.api.get(path)).json()["describe"] == {
            "model": "company-vision",
            "hint": "",
        }
        # A rename keeps it; null turns it off; names cannot be null.
        renamed = await p.api.patch(path, json={"name": "改名"})
        assert renamed.json()["describe"]["model"] == "company-vision"
        off = await p.api.patch(path, json={"describe": None})
        assert off.status_code == 200 and off.json()["describe"] is None
        null_name = await p.api.patch(path, json={"name": None})
        assert null_name.status_code == 422
        # While the gateway's models cannot be read, the setting is taken.
        app.state.models = None
        try:
            taken = await p.api.patch(path, json={"describe": {"model": "nope"}})
        finally:
            app.state.models = Models()
        assert taken.status_code == 200, taken.text


def test_an_answer_becomes_one_line():
    assert clean(" 「卧推」\n\n侧面 ") == "卧推」 侧面"
    assert clean("长" * 600) == "长" * 500


# The media_search node


def search_flow(library: str, access: str = "read", **node: Any) -> dict:
    footage = {
        "id": "footage",
        "type": "media_search",
        "title": "教练素材",
        "library": library,
        "query": "{{start.topic}}",
        **node,
    }
    return {
        "media": {"libraries": [{"library_id": library, "access": access}]},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [{"key": "topic", "label": "找什么", "type": "text"}],
                },
                footage,
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [
                        {"key": "items", "from": ["{{footage.items}}"]},
                        {"key": "count", "from": ["{{footage.count}}"]},
                        {"key": "total", "from": ["{{footage.total}}"]},
                    ],
                },
            ],
            "edges": [{"from": "start", "to": "footage"}, {"from": "footage", "to": "end"}],
        },
    }


async def footage(p: Media) -> dict[str, str]:
    """A few items of the coach's library, by name."""
    made = {}
    for name, kwargs, description, tags in [
        ("卧推侧面.mp4", {}, "健身房杠铃卧推，侧面全景", ["胸"]),
        ("卧推正面.mp4", {}, "健身房杠铃卧推，正面特写", ["胸", "特写"]),
        ("卧推照片.jpg", {}, "杠铃卧推的照片", ["胸"]),
        ("卧推配音.m4a", {}, "讲卧推的配音", ["胸"]),
        ("卧推坏了.mp4", {"status": "error"}, "杠铃卧推", ["胸"]),
        ("卧推不用.mp4", {"usable": False}, "杠铃卧推", ["胸"]),
        ("深蹲.mp4", {}, "深蹲，侧面", ["腿"]),
    ]:
        item = await p.add(p.footage, name, **kwargs)
        await p.media.update_item(p.footage, item.item_id, description=description, tags=tags)
        made[name] = item.item_id
    return made


async def test_the_node_lists_usable_items_with_their_descriptions(tmp_path):
    async with Media(tmp_path) as p:
        made = await footage(p)
        await p.new_app("coach", search_flow(p.footage, kinds=["video", "image"], limit=1))
        run = await p.run("coach", {"topic": "杠铃卧推"})
        assert run["status"] == "done", run["error"]
        [item] = run["result"]["items"]
        assert (run["result"]["count"], run["result"]["total"]) == (1, 3)
        name = item["name"]
        assert name in ("卧推侧面.mp4", "卧推正面.mp4", "卧推照片.jpg")
        assert item == {
            "item_id": made[name],
            "library_id": p.footage,
            "name": name,
            "kind": "image" if name.endswith(".jpg") else "video",
            "duration_seconds": None if name.endswith(".jpg") else 48.2,
            "width": None,
            "height": None,
            "path": f"{p.footage}/{made[name]}/{name}",
            "orientation": None,
            "description": item["description"],
            "tags": item["tags"],
            "source": {"kind": "upload"},
        }

        # Every word, the tags (all of them), the kinds; nothing found is no failure.
        config = search_flow(p.footage, tags=["胸", "特写"], kinds=["video"])
        await p.new_app("close", config)
        run = await p.run("close", {"topic": "卧推"})
        assert [i["name"] for i in run["result"]["items"]] == ["卧推正面.mp4"]
        await p.new_app("all", search_flow(p.footage, query=""))
        names = {i["name"] for i in (await p.run("all", {"topic": ""}))["result"]["items"]}
        assert names == {"卧推侧面.mp4", "卧推正面.mp4", "卧推照片.jpg", "卧推配音.m4a", "深蹲.mp4"}
        run = await p.run("coach", {"topic": "游泳"})
        assert run["status"] == "done"
        assert (run["result"]["items"], run["result"]["count"]) == ([], 0)

        # Tried alone with the draft's settings.
        tried = await p.api.post(
            "/apps/close/nodes/footage/test", json={"values": {"start.topic": "深蹲"}}
        )
        assert tried.json()["status"] == "done", tried.text
        assert tried.json()["outputs"]["count"] == 0

        # A library deleted since fails the node.
        await p.media.delete_library(p.footage)
        run = await p.run("coach", {"topic": "卧推"})
        assert run["status"] == "error"
        assert f"媒体库不见了：{p.footage}" in run["error"]


async def test_the_node_is_checked_when_saved(tmp_path):
    async with Media(tmp_path) as p:
        resp = await p.api.post("/apps", json={"app": "coach", "title": "教练做视频"})
        assert resp.status_code == 201, resp.text

        async def problems(config: dict) -> dict[tuple, str]:
            resp = await p.api.put("/apps/coach/draft", json={"config": config})
            if resp.status_code == 200:
                return {}
            assert resp.status_code == 422, resp.text
            return {tuple(d["loc"][4:]): d["msg"] for d in resp.json()["detail"]}

        assert await problems(search_flow(p.footage)) == {}
        assert await problems(search_flow(p.footage, access="write")) == {}
        unbound = search_flow(p.footage)
        unbound["media"]["libraries"][0]["library_id"] = p.voices
        node = ("footage",)
        assert await problems(unbound) == {node + ("library",): "要选一个绑定到应用的媒体库"}
        found = await problems(
            search_flow(
                p.footage, query="长" * 201, kinds=["video", "film"], tags=["好" * 21], limit=0
            )
        )
        assert found == {
            node + ("query",): "搜索词最多 200 个字符",
            node + ("kinds",): "素材类型只能是 video、audio、image",
            node + ("tags",): "标签最多 20 个，每个 1–20 个字",
            node + ("limit",): "最多取几个要在 1–200 之间",
        }
        assert await problems(search_flow("")) != {}

        listed = (await p.api.get("/node-types")).json()
        spec = next(t for t in listed if t["type"] == "media_search")
        assert (spec["label"], spec["category"]) == ("检索素材", "tool")
        assert [o["key"] for o in spec["outputs"]] == ["items", "count", "total"]
        assert spec["defaults"]["kinds"] == ["video", "image"]
