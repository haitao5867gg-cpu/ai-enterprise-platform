"""Images an llm node sends to a model that can see them, and models marked as able to
(docs/api/vision.md)."""

import base64
import json
from typing import Any

import pytest

from tests.test_app_media import Media
from tests.test_media_processing import Processing
from tests.test_models import CLAUDE, Platform

PIXELS = bytes(range(100))  # what Media.add writes into each file


def look_flow(footage: str, **node: Any) -> dict:
    """A photo from ``footage`` and frames from elsewhere, looked at by a model."""
    look = {
        "id": "look",
        "type": "llm",
        "title": "看画面",
        "model": "mock",
        "prompt": "这个镜头是{{start.topic}}吗？",
        "images": ["{{start.photo}}", "{{start.frames}}"],
        **node,
    }
    return {
        "media": {"libraries": [{"library_id": footage, "access": "read"}]},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {
                            "key": "photo",
                            "label": "照片",
                            "type": "media",
                            "required": True,
                            "library": footage,
                        },
                        {"key": "frames", "label": "抽出的帧", "type": "text"},
                        {"key": "topic", "label": "主题", "type": "text"},
                    ],
                },
                look,
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "answer", "from": ["{{look.text}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "look"}, {"from": "look", "to": "end"}],
        },
    }


def chats(p: Media) -> list[dict]:
    return [r["body"] for r in p.gateway.requests if r["url"].endswith("/chat/completions")]


def data_url(kind: str, data: bytes = PIXELS) -> dict:
    url = f"data:{kind};base64,{base64.b64encode(data).decode()}"
    return {"type": "image_url", "image_url": {"url": url}}


def frames(*paths: str) -> dict:
    """What the media connector's frames tool puts out (docs/api/media-processing.md)."""
    return {
        "frames": [
            {"time": float(i), "width": 64, "height": 64,
             "image": {"aep_file": {"path": path, "name": path.rsplit("/", 1)[-1]}}}
            for i, path in enumerate(paths)
        ]
    }  # fmt: skip


def work_file(p: Media, name: str, data: bytes = PIXELS) -> str:
    work = p.settings.media_dir / "_work" / "j_1"
    work.mkdir(parents=True, exist_ok=True)
    (work / name).write_bytes(data)
    return f"_work/j_1/{name}"


async def test_a_photo_and_the_frames_a_tool_made_go_after_the_text(tmp_path):
    async with Processing(tmp_path) as p:
        config = look_flow(p.footage, images=["{{start.photo}}", "{{make.result}}"])
        nodes, edges = config["graph"]["nodes"], config["graph"]["edges"]
        files = [{"name": "f1.jpg", "size": 3}, {"name": "f2.webp", "size": 4}]
        nodes.insert(1, {"id": "make", "type": "tool", "title": "抽帧", "datasource":
                         p.processing, "tool": "make", "args": {"files": files}})  # fmt: skip
        edges[:1] = [{"from": "start", "to": "make"}, {"from": "make", "to": "look"}]
        await p.new_app("look", config)
        photo = await p.add(p.footage, "教练.png")
        run = await p.run("look", {"photo": photo.item_id, "topic": "深蹲"})
        assert run["status"] == "done", run["error"]
        [sent] = chats(p)
        assert sent["model"] == "mock"
        # make.result names f1.jpg as its video and again in its list: sent once.
        assert sent["messages"][-1] == {
            "role": "user",
            "content": [
                {"type": "text", "text": "这个镜头是深蹲吗？"},
                data_url("image/png"),
                data_url("image/jpeg", b"\x00" * 3),
                data_url("image/webp", b"\x00" * 4),
            ],
        }
        # The run's messages stay text: the images are not kept in them.
        assert run["messages"][-1]["role"] == "assistant"
        assert all(
            isinstance(m["content"], str) and "base64" not in m["content"] for m in run["messages"]
        )


async def test_frames_inside_a_value_are_found_in_order(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("look", look_flow(p.footage))
        photo = await p.add(p.footage, "教练.png")
        first, second = work_file(p, "f1.jpg"), work_file(p, "f2.webp", b"webp")
        values = {"start.photo": photo.rel_path, "start.frames": frames(first, second)}
        resp = await p.api.post("/apps/look/nodes/look/test", json={"values": values})
        assert resp.json()["status"] == "done", resp.json()
        [sent] = chats(p)
        assert sent["messages"][-1]["content"][1:] == [
            data_url("image/png"),
            data_url("image/jpeg"),
            data_url("image/webp", b"webp"),
        ]


async def test_media_paths_and_summaries_are_sent_and_json_retries_keep_the_images(tmp_path):
    async with Media(tmp_path) as p:
        output = {"kind": "json", "fields": [{"key": "action", "type": "string"}]}
        config = look_flow(p.footage, output=output)
        config["graph"]["nodes"][-1]["outputs"] = [{"key": "action", "from": ["{{look.action}}"]}]
        await p.new_app("look", config)
        photo = await p.add(p.footage, "教练.jpeg")
        other = await p.add(p.footage, "侧面.webp")
        summary = {"item_id": other.item_id, "path": other.rel_path, "kind": "image"}
        p.gateway.replies = ["不是 JSON", json.dumps({"action": "深蹲"})]
        resp = await p.api.post(
            "/apps/look/nodes/look/test",
            json={
                "values": {
                    "start.photo": photo.rel_path,
                    "start.frames": [summary],
                    "start.topic": "深蹲",
                }
            },
        )
        tried = resp.json()
        assert tried["status"] == "done", tried
        assert tried["outputs"] == {"action": "深蹲"}
        first, retry = chats(p)
        images = [data_url("image/jpeg"), data_url("image/webp")]
        assert first["messages"][-1]["content"][1:] == images
        # The retry repeats the question, images included, then says what was wrong.
        assert retry["messages"][-3] == first["messages"][-1]
        assert retry["messages"][-1]["content"].startswith("回复不符合要求")


async def test_no_images_send_text_as_before(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("look", look_flow(p.footage, images=[]))
        photo = await p.add(p.footage, "教练.png")
        run = await p.run("look", {"photo": photo.item_id, "topic": "深蹲"})
        assert run["status"] == "done", run["error"]
        [sent] = chats(p)
        assert sent["messages"][-1] == {"role": "user", "content": "这个镜头是深蹲吗？"}


@pytest.mark.parametrize(
    ("photo", "extra", "message"),
    [
        (42, None, "第 1 项图片取到的不是文件：42"),
        ("OK", {"time": 1}, "第 2 项图片取到的不是文件"),
        ("../x.png", None, "文件不在媒体库里：../x.png"),
        ("/etc/passwd", None, "文件不在媒体库里：/etc/passwd"),
        ("LINK", None, "文件不在媒体库里"),
        ("_work/none.png", None, "文件不见了：_work/none.png"),
        ("VOICE", None, "只能发图片：配音.m4a"),
        ("BIG", None, "图片「大.png」超过 1 MB"),
        ("OK", "THREE", "一次最多发 3 张图片，取到了 4 张"),
    ],
)
async def test_what_a_node_refuses_to_send(tmp_path, photo, extra, message):
    async with Media(tmp_path, vision_max_mb=1, vision_max_images=3) as p:
        await p.new_app("look", look_flow(p.footage))
        good = await p.add(p.footage, "教练.png")
        root = p.settings.media_dir
        if photo == "OK":
            photo = good.rel_path
        elif photo == "LINK":
            (root / "_work").mkdir(parents=True)
            (root / "_work" / "link.png").symlink_to(root / good.rel_path)
            photo = "_work/link.png"
        elif photo == "VOICE":
            photo = (await p.add(p.voices, "配音.m4a")).rel_path
        elif photo == "BIG":
            photo = work_file(p, "大.png", b"\x00" * (1024 * 1024 + 1))
        if extra == "THREE":
            extra = frames(*(work_file(p, f"f{i}.jpg") for i in range(3)))
        values = {"start.photo": photo, "start.topic": "深蹲"}
        if extra is not None:
            values["start.frames"] = extra
        resp = await p.api.post("/apps/look/nodes/look/test", json={"values": values})
        tried = resp.json()
        assert tried["status"] == "error", tried
        assert tried["error"]["type"] == "input_error"
        assert message in tried["error"]["message"]
        assert chats(p) == []


async def test_a_run_whose_policy_keeps_images_in_sends_nothing(tmp_path):
    async with Media(tmp_path, data_media="audio") as p:
        await p.new_app("look", look_flow(p.footage))
        photo = await p.add(p.footage, "教练.png")
        run = await p.run("look", {"photo": photo.item_id, "topic": "深蹲"})
        assert run["status"] == "error"
        assert run["error"] == "节点「看画面」(look)：平台不允许把图片发给模型，节点「看画面」要发"
        assert chats(p) == []


async def test_images_are_checked_when_saved(tmp_path):
    async with Media(tmp_path) as p:
        resp = await p.api.post("/apps", json={"app": "look", "title": "看画面"})
        assert resp.status_code == 201, resp.text
        shape = "图片要写最多 10 项，每项一个引用或媒体路径"
        for images, where in (
            ("{{start.photo}}", ()),
            (["{{start.photo}}"] * 11, ()),
            (["{{start.photo}}", " "], (1,)),
            ([3], (0,)),
        ):
            config = look_flow(p.footage, images=images)
            resp = await p.api.put("/apps/look/draft", json={"config": config})
            assert resp.status_code == 422, resp.text
            [problem] = resp.json()["detail"]
            assert (tuple(problem["loc"][5:]), problem["msg"]) == (("images", *where), shape)
        config = look_flow(p.footage, images=["{{nope.image}}"])
        resp = await p.api.put("/apps/look/draft", json={"config": config})
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"][5:] == ["images", 0]


VISION_MODEL = {
    "model_name": "company-vision",
    "litellm_params": {"model": "openai/qwen3.6-flash", "api_base": "https://aigw.example.com/v1"},
    "model_info": {
        "id": "cfg-company-vision",
        "db_model": False,
        "aep_display_name": "公司网关看图模型",
        "aep_purpose": "chat",
        "aep_provider": "openai_compatible",
        "supports_vision": True,
    },
}


async def test_models_that_can_see_images_are_marked():
    async with Platform() as p:
        p.fake.deployments.append(VISION_MODEL)
        listing = (await p.api.get("/models")).json()
        choices = {c["alias"]: c for c in await p.models.choices()}
        made = await p.api.post("/models", json={**CLAUDE, "vision": True})
        assert made.status_code == 201, made.text
        info = p.fake.by_alias("claude-sonnet")["model_info"]
        assert info["supports_vision"] is True
        turned_off = await p.api.patch("/models/claude-sonnet", json={"vision": False})
        embed = {**CLAUDE, "alias": "embed2", "purpose": "embedding", "vision": True}
        refused = await p.api.post("/models", json=embed)
    vision = {m["alias"]: m["vision"] for m in listing["models"]}
    assert vision["company-vision"] is True
    assert vision["company"] is False and vision["company-embed"] is False
    assert choices["company-vision"]["vision"] is True and choices["company"]["vision"] is False
    assert made.json()["vision"] is True
    assert turned_off.status_code == 200, turned_off.text
    assert turned_off.json()["vision"] is False
    assert refused.status_code == 422
    assert refused.json()["detail"] == "只有对话模型能设为能看图"
