"""Media libraries in apps: binding them, media inputs, the app API's media endpoints and
the files a run made (docs/api/media.md, "应用用媒体库")."""

import copy
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from aep.app_access import ApiKeys, MemoryApiKeyStore
from aep.auth import LABELS, required, runtime_allows
from aep.config import get_settings
from aep.main import app
from aep.media.files import KINDS
from aep.media.store import MediaItem
from aep.runtime.flow import nodes
from aep.runtime.flow.schema import input_schema
from tests.conftest import SETTINGS
from tests.test_app_access import mcp_client
from tests.test_flow_tools import Platform
from tests.test_packages import Packaging
from tests.test_runtime_mode import Machine, item

UPLOAD = "/v1/apps/{app}/media-libraries/{library_id}/items"


class Media(Platform):
    """The tools-test platform with two media libraries, files on a temporary disk and
    API keys."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__([])
        self.settings = SETTINGS.model_copy(
            update={"media_dir": tmp_path / "media", "media_max_file_mb": 1}
        )
        self.media = self.service.media
        self.keys = ApiKeys(MemoryApiKeyStore())
        self.app = app

    async def __aenter__(self) -> "Media":
        await super().__aenter__()
        app.state.media = self.media
        app.state.api_keys = self.keys
        app.state.web_url = "https://aep.example.com"
        app.dependency_overrides[get_settings] = lambda: self.settings
        self.voices = (await self.media.create_library("配音")).library_id
        self.footage = (await self.media.create_library("教练素材")).library_id
        return self

    async def __aexit__(self, *exc: Any) -> None:
        app.dependency_overrides.pop(get_settings, None)
        await super().__aexit__(*exc)

    async def add(
        self,
        library_id: str,
        name: str,
        *,
        status: str = "ready",
        source: dict[str, Any] | None = None,
        usable: bool = True,
    ) -> MediaItem:
        item_id = str(uuid.uuid4())
        rel = f"{library_id}/{item_id}/{name}"
        path = self.settings.media_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(range(100)))
        kind = KINDS[Path(name).suffix]
        item = MediaItem(
            item_id,
            library_id,
            name,
            kind,
            100,
            rel,
            status=status,  # type: ignore[arg-type]
            duration_seconds=None if kind == "image" else 48.2,
            usable=usable,
            source=source or {"kind": "upload"},
        )
        [added] = await self.media.add_items(library_id, [item], 100)
        return added

    async def key(self, app_name: str = "coach", name: str = "教练页面") -> dict[str, str]:
        _, secret = await self.keys.create(app_name, name, "王搭建")
        return {"Authorization": f"Bearer {secret}"}


def coach_flow(voices: str, footage: str, **voiceover: Any) -> dict:
    """Takes a voice-over from ``voices`` and puts out what the start node made of it."""
    field = {
        "key": "voiceover",
        "label": "配音",
        "type": "media",
        "required": True,
        "library": voices,
        "kinds": ["audio"],
        **voiceover,
    }
    return {
        "media": {
            "libraries": [
                {"library_id": voices, "access": "write"},
                {"library_id": footage, "access": "read"},
            ]
        },
        "graph": {
            "nodes": [
                {"id": "start", "type": "start", "title": "开始", "inputs": [field]},
                {
                    "id": "say",
                    "type": "template",
                    "title": "说明",
                    "template": "配音是 {{start.voiceover}}",
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [
                        {"key": "voice", "from": ["{{start.voiceover}}"]},
                        {"key": "said", "from": ["{{say.text}}"]},
                    ],
                },
            ],
            "edges": [{"from": "start", "to": "say"}, {"from": "say", "to": "end"}],
        },
    }


async def problems(p: Media, config: dict) -> dict[tuple, str]:
    resp = await p.api.put("/apps/coach/draft", json={"config": config})
    assert resp.status_code == 422, resp.text
    return {tuple(d["loc"][2:]): d["msg"] for d in resp.json()["detail"]}


# Binding and the media input


async def test_binding_and_media_inputs_are_checked_when_saved(tmp_path):
    async with Media(tmp_path) as p:
        resp = await p.api.post("/apps", json={"app": "coach", "title": "教练做视频"})
        assert resp.status_code == 201, resp.text
        good = coach_flow(p.voices, p.footage)
        assert (await p.api.put("/apps/coach/draft", json={"config": good})).status_code == 200

        bad = copy.deepcopy(good)
        libraries = bad["media"]["libraries"]
        libraries[1] = {"library_id": str(uuid.uuid4()), "access": "read"}
        libraries.append({"library_id": p.voices, "access": "owner"})
        assert await problems(p, bad) == {
            ("media", "libraries", 1, "library_id"): "没有这个媒体库",
            (
                "media",
                "libraries",
                2,
                "access",
            ): "权限只能是 read（只读）或 write（可以上传和修改）",
            ("media", "libraries", 2, "library_id"): f"媒体库 {p.voices} 绑定了两次",
        }

        many = copy.deepcopy(good)
        many["media"]["libraries"] = [
            {"library_id": str(uuid.uuid4()), "access": "read"} for _ in range(11)
        ]
        assert (await problems(p, many))[("media", "libraries")] == "最多绑定 10 个媒体库"

        unbound = coach_flow(p.footage, p.footage)
        unbound["media"]["libraries"] = [{"library_id": p.voices, "access": "write"}]
        where = ("graph", "nodes", "start", "inputs", 0)
        assert await problems(p, unbound) == {
            (*where, "library"): "素材输入要选一个绑定到应用的媒体库"
        }
        wrong = coach_flow(p.voices, p.footage, kinds=["audio", "audio"], default="x")
        assert await problems(p, wrong) == {
            (*where, "kinds"): "素材类型只能是 video、audio、image",
            (*where, "default"): "素材输入不能设默认值",
        }
        nameless = coach_flow(p.voices, p.footage, library="")
        assert await problems(p, nameless) == {
            (*where, "library"): "素材输入要选一个绑定到应用的媒体库"
        }

        asking = copy.deepcopy(good)
        asking["graph"]["nodes"].insert(
            2,
            {
                "id": "ask",
                "type": "human",
                "title": "补一段",
                "mode": "form",
                "prompt": "再给一段",
                "fields": [{"key": "more", "label": "再一段", "type": "media"}],
            },
        )
        asking["graph"]["edges"] = [
            {"from": "start", "to": "say"},
            {"from": "say", "to": "ask"},
            {"from": "ask", "to": "end"},
        ]
        found = await problems(p, asking)
        assert found[("graph", "nodes", "ask", "fields", 0, "type")] == (
            "只有开始节点的输入可以是素材"
        )


async def test_a_media_input_takes_an_item_and_puts_out_its_summary(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("coach", coach_flow(p.voices, p.footage))
        voice = await p.add(p.voices, "配音0115.m4a", usable=False)
        run = await p.run("coach", {"voiceover": voice.item_id})
        assert run["status"] == "done", run["error"]
        summary = {
            "item_id": voice.item_id,
            "library_id": p.voices,
            "name": "配音0115.m4a",
            "kind": "audio",
            "duration_seconds": 48.2,
            "width": None,
            "height": None,
            "path": f"{p.voices}/{voice.item_id}/配音0115.m4a",
        }
        # A whole reference gets the summary itself; in text it is its JSON.
        assert run["result"]["voice"] == summary
        assert run["result"]["said"].startswith('配音是 {"item_id": "')
        # The run keeps the id it was given.
        assert run["input"] == {"voiceover": voice.item_id}


async def test_what_a_media_input_refuses(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("coach", coach_flow(p.voices, p.footage))
        clip = await p.add(p.voices, "卧推.mp4")
        busy = await p.add(p.voices, "新配音.m4a", status="processing")
        broken = await p.add(p.voices, "坏了.m4a", status="error")
        elsewhere = await p.add(p.footage, "别处.m4a")

        async def refused(value: Any) -> str:
            resp = await p.api.post("/runs", json={"app": "coach", "input": {"voiceover": value}})
            assert resp.status_code == 422, resp.text
            [detail] = resp.json()["detail"]
            assert detail["loc"] == ["body", "input", "voiceover"]
            return detail["msg"]

        assert await refused(42) == "配音要是素材 id"
        assert await refused(elsewhere.item_id) == "配音：媒体库「配音」里没有这个素材"
        assert await refused("nope") == "配音：媒体库「配音」里没有这个素材"
        assert await refused(clip.item_id) == "配音要是音频"
        assert await refused(busy.item_id) == "配音：素材还在处理，稍后再试"
        assert await refused(broken.item_id) == "配音：素材处理失败了，不能用"
        assert await refused(None) == "配音不能为空"

        await p.api.put(
            "/apps/coach/draft",
            json={"config": coach_flow(p.voices, p.footage, kinds=["video", "audio"])},
        )
        resp = await p.api.post(
            "/runs", json={"app": "coach", "draft": True, "input": {"voiceover": "x"}}
        )
        assert resp.status_code == 422
        photo = await p.add(p.voices, "封面.jpg")
        resp = await p.api.post(
            "/runs", json={"app": "coach", "draft": True, "input": {"voiceover": photo.item_id}}
        )
        assert resp.json()["detail"][0]["msg"] == "配音要是视频或音频"


async def test_an_item_deleted_before_the_run_starts_fails_the_start(tmp_path):
    async with Media(tmp_path) as p:
        voice = await p.add(p.voices, "配音.m4a")
        start = coach_flow(p.voices, p.footage)["graph"]["nodes"][0]
        rt = nodes.Runtime(p.gateway.client, "mock", {}, {}, media=p.media)
        state = {"input": {"voiceover": voice.item_id}}
        made = await nodes.run_start(start, state, rt)
        assert made.outputs["voiceover"]["item_id"] == voice.item_id
        await p.media.delete_item(p.voices, voice.item_id)
        with pytest.raises(nodes.NodeFailure) as failed:
            await nodes.run_start(start, state, rt)
        assert failed.value.type == nodes.INPUT_ERROR
        assert failed.value.message == "配音：素材已经被删除"


def test_mcp_tools_take_media_inputs_as_ids() -> None:
    schema = input_schema(coach_flow("lib", "lib2")["graph"]["nodes"][0]["inputs"])
    assert schema["properties"]["voiceover"] == {
        "description": "配音（素材 id，先经 /v1 上传）",
        "type": "string",
    }
    assert schema["required"] == ["voiceover"]


# The app API


def test_the_app_api_media_routes_answer_to_keys() -> None:
    path = UPLOAD.replace("{library_id}", "{library_id}")
    assert required("POST", path) is None
    assert runtime_allows("POST", path)
    assert LABELS[f"POST {path}"] == "经 API 上传素材"
    assert LABELS[f"DELETE {path}/{{item_id}}"] == "经 API 删除素材"


async def test_keys_list_upload_change_and_download_bound_items(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("coach", coach_flow(p.voices, p.footage))
        headers = await p.key()
        listed = await p.api.get("/v1/apps/coach/media-libraries", headers=headers)
        assert listed.status_code == 200, listed.text
        assert [(lib["name"], lib["access"]) for lib in listed.json()] == [
            ("配音", "write"),
            ("教练素材", "read"),
        ]

        upload = UPLOAD.format(app="coach", library_id=p.voices)
        resp = await p.api.post(
            upload,
            files=[("files", ("配音0115.m4a", b"\x00" * 64))],
            data={"description": "第一段", "tags": '["减脂"]', "uploaded_by": "冒充的人"},
            headers=headers,
        )
        assert resp.status_code == 202, resp.text
        [made] = resp.json()
        assert made["uploaded_by"] == "API：教练页面" and made["tags"] == ["减脂"]
        assert made["status"] == "ready" and made["source"]["kind"] == "upload"

        page = await p.api.get(f"{upload}?q=第一段", headers=headers)
        assert page.json()["total"] == 1
        one = f"{upload}/{made['item_id']}"
        assert (await p.api.get(one, headers=headers)).json()["name"] == "配音0115.m4a"
        changed = await p.api.patch(one, json={"usable": False}, headers=headers)
        assert changed.status_code == 200 and changed.json()["usable"] is False
        part = await p.api.get(f"{one}/file", headers={**headers, "Range": "bytes=0-9"})
        assert part.status_code == 206 and part.content == b"\x00" * 10

        # The run takes the uploaded item.
        resp = await p.api.post(
            "/v1/apps/coach/runs",
            json={"input": {"voiceover": made["item_id"]}, "wait": 5},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["result"]["voice"]["name"] == "配音0115.m4a"

        assert (await p.api.delete(one, headers=headers)).status_code == 204
        assert (await p.api.get(one, headers=headers)).status_code == 404


async def test_what_the_app_api_refuses(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("coach", coach_flow(p.voices, p.footage))
        await p.new_app("other", coach_flow(p.voices, p.footage))
        headers = await p.key()
        clip = await p.add(p.footage, "卧推.mp4")
        footage = UPLOAD.format(app="coach", library_id=p.footage)

        # Read-only: listing and downloading only.
        assert (await p.api.get(footage, headers=headers)).json()["total"] == 1
        file = await p.api.get(f"{footage}/{clip.item_id}/file", headers=headers)
        assert file.status_code == 200 and file.content == bytes(range(100))
        for method, path in (
            ("POST", footage),
            ("PATCH", f"{footage}/{clip.item_id}"),
            ("DELETE", f"{footage}/{clip.item_id}"),
        ):
            body = {"json": {"usable": False}} if method == "PATCH" else {}
            resp = await p.api.request(method, path, headers=headers, **body)
            assert resp.status_code == 403, (method, resp.text)
            assert resp.json()["detail"] == "这个应用对媒体库「教练素材」只有读权限"

        unbound = (await p.media.create_library("别的库")).library_id
        resp = await p.api.get(UPLOAD.format(app="coach", library_id=unbound), headers=headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "这个应用没有绑定这个媒体库"

        resp = await p.api.get("/v1/apps/other/media-libraries", headers=headers)
        assert resp.status_code == 403
        assert (await p.api.get("/v1/apps/coach/media-libraries")).status_code == 401

        resp = await p.api.put("/apps/coach/access", json={"api": False})
        assert resp.status_code == 200, resp.text
        resp = await p.api.get(footage, headers=headers)
        assert resp.status_code == 403 and resp.json()["detail"] == "这个应用没有开放这个入口"


# Files a run made


async def test_runs_list_the_files_they_made(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("coach", coach_flow(p.voices, p.footage))
        headers = await p.key()
        voice = await p.add(p.voices, "配音.m4a")
        resp = await p.api.post(
            "/v1/apps/coach/runs",
            json={"input": {"voiceover": voice.item_id}, "wait": 5},
            headers=headers,
        )
        run = resp.json()
        assert run["files"] == []
        run_id = run["run_id"]
        made = await p.add(p.footage, "成片.mp4", source={"kind": "run", "run_id": run_id})
        await p.add(p.voices, "别的运行.mp4", source={"kind": "run", "run_id": "r-other"})
        unbound = (await p.media.create_library("没绑定")).library_id
        await p.add(unbound, "不列.mp4", source={"kind": "run", "run_id": run_id})

        got = (await p.api.get(f"/v1/runs/{run_id}", headers=headers)).json()
        path = f"/v1/apps/coach/media-libraries/{p.footage}/items/{made.item_id}/file"
        assert got["files"] == [
            {
                "item_id": made.item_id,
                "library_id": p.footage,
                "name": "成片.mp4",
                "kind": "video",
                "size_bytes": 100,
                "duration_seconds": 48.2,
                "width": None,
                "height": None,
                "download_path": path,
            }
        ]
        assert (await p.api.get(path, headers=headers)).status_code == 200

        # The run page lists the same files, served by the platform's media API.
        shown = (await p.api.get(f"/runs/{run_id}/media")).json()
        own = f"/media-libraries/{p.footage}/items/{made.item_id}/file"
        assert shown == [got["files"][0] | {"download_path": own}]
        assert (await p.api.get(own)).status_code == 200
        assert (await p.api.get("/runs/nope/media")).status_code == 404

        secret = headers["Authorization"].removeprefix("Bearer ")
        async with mcp_client(p, "coach", secret) as client:  # type: ignore[arg-type]
            got_run: Any = await client.call_tool("get_run", {"run_id": run_id})
            assert got_run.structured_content["files"][0]["item_id"] == made.item_id


# Files, packages and runtimes


async def test_exports_name_the_libraries_and_imports_report_missing_ones(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("coach", coach_flow(p.voices, p.footage))
        exported = await p.api.get("/apps/coach/versions/2/export")
        assert exported.status_code == 200, exported.text
        data = yaml.safe_load(exported.text)
        assert data["config"]["media"]["libraries"][0] == {
            "library_id": p.voices,
            "access": "write",
        }
        assert data["requires"]["media_libraries"] == [
            {"library_id": p.voices, "name": "配音", "access": "write"},
            {"library_id": p.footage, "name": "教练素材", "access": "read"},
        ]

        await p.media.delete_library(p.footage)
        resp = await p.api.post("/apps/import", json={"app": "coach-copy", "yaml": exported.text})
        assert resp.status_code == 201, resp.text
        assert resp.json()["problems"] == [
            {
                "kind": "media_library",
                "library_id": p.footage,
                "name": "教练素材",
                "message": "没有这个媒体库，请新建一个或在配置里换掉",
            }
        ]


async def test_a_runtime_makes_the_libraries_a_package_binds():
    async with Packaging() as p:
        voices = (await p.service.media.create_library("配音")).library_id
        footage = (await p.service.media.create_library("教练素材")).library_id
        await p.new_app("coach", coach_flow(voices, footage))
        needs = (await p.api.get("/apps/coach/versions/2/requirements")).json()
        assert needs["unsupported"] is None
        assert needs["media_libraries"] == [
            {"library_id": voices, "name": "配音", "access": "write"},
            {"library_id": footage, "name": "教练素材", "access": "read"},
        ]
        got = await p.api.get("/apps/coach/versions/2/package")
        assert got.status_code == 200, got.text
        package = got.json()

    m = Machine()
    # A library of that name is already there: the package's gets the app's name too.
    await m.service.media.create_library("配音")
    resp = await m.install(package)
    assert resp.status_code == 201, resp.text
    names = {lib.library_id: lib.name for lib in await m.service.media.libraries()}
    assert names[voices] == "配音（coach）" and names[footage] == "教练素材"
    check = resp.json()["check"]
    assert item(check, "media_library", "教练素材")["ok"]
    assert item(check, "media_library", "配音（coach）")["library_id"] == voices

    await m.service.media.delete_library(footage)
    check = (await m.api.get("/runtime/apps/coach/check?fresh=true")).json()
    gone = item(check, "media_library", "教练素材")
    assert not gone["ok"] and gone["message"] == "没有这个媒体库，请重新装一次流程包"
    assert "媒体库「教练素材」" in (await m.runtime.ready("coach") or "")
    # Installing the same package again changes nothing; a new install would make it again.
    assert (await m.install(package)).status_code == 200
