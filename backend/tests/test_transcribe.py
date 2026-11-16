"""The transcribe node, transcription models and the media kind of the data outbound policy
(docs/api/transcription.md)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from aep import llm
from aep.config import Settings
from aep.models import MemoryCallStore
from aep.policy import DataPolicy, media_problem, platform_default
from aep.projects import effective_policy
from tests.test_app_media import Media
from tests.test_models import Platform
from tests.test_projects import project


def asr_flow(voices: str, **node: Any) -> dict:
    """A voice-over from ``voices``, transcribed."""
    asr = {
        "id": "asr",
        "type": "transcribe",
        "title": "转写配音",
        "model": "mock",
        "audio": "{{start.voiceover}}",
        "language": "zh",
        "prompt": "以下是普通话的健身教学：{{start.topic}}",
        **node,
    }
    return {
        "media": {"libraries": [{"library_id": voices, "access": "write"}]},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {
                            "key": "voiceover",
                            "label": "配音",
                            "type": "media",
                            "required": True,
                            "library": voices,
                        },
                        {"key": "topic", "label": "主题", "type": "text"},
                    ],
                },
                asr,
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": k, "from": [f"{{{{asr.{k}}}}}"]} for k in HEARD],
                },
            ],
            "edges": [{"from": "start", "to": "asr"}, {"from": "asr", "to": "end"}],
        },
    }


HEARD = {
    "text": "冬天减脂比夏天难吗？ 关键是热量缺口。",
    "language": "zh",
    "duration_seconds": 4.771,
    "segments": [
        {"start": 0.0, "end": 2.28, "text": "冬天减脂比夏天难吗？"},
        {"start": 2.78, "end": 4.12, "text": "关键是热量缺口。"},
    ],
    "words": [
        {"start": 0.0, "end": 0.28, "word": "冬天"},
        {"start": 0.28, "end": 0.62, "word": "减脂"},
        {"start": 2.78, "end": 3.1, "word": "关键"},
    ],
}


def transcriptions(p: Media) -> list[dict]:
    return [r["body"] for r in p.gateway.requests if r["url"].endswith("/audio/transcriptions")]


async def test_a_voiceover_becomes_timed_text(tmp_path):
    calls = MemoryCallStore()
    llm.set_recorder(calls.record)
    try:
        async with Media(tmp_path) as p:
            await p.new_app("asr", asr_flow(p.voices))
            voice = await p.add(p.voices, "配音.m4a")
            run = await p.run("asr", {"voiceover": voice.item_id, "topic": "冬天减脂"})
            assert run["status"] == "done", run["error"]
            assert run["result"] == HEARD
            [sent] = transcriptions(p)
            assert sent["file"] == {"filename": "配音.m4a", "size": 100}
            assert sent["model"] == "mock" and sent["language"] == "zh"
            assert sent["response_format"] == "verbose_json"
            assert sent["timestamp_granularities[]"] == ["segment", "word"]
            assert sent["prompt"] == "以下是普通话的健身教学：冬天减脂"
    finally:
        llm.set_recorder(None)
    [(_, call)] = calls.calls
    assert (call.kind, call.ok, call.audio_seconds) == ("transcription", True, 4.771)
    assert call.run_id == run["run_id"] and call.node == "asr"
    summary = await calls.summary(datetime(2000, 1, 1, tzinfo=UTC))
    assert summary["mock"].audio_seconds == 4.771


async def test_files_a_tool_made_and_media_paths_transcribe_too(tmp_path):
    async with Media(tmp_path) as p:
        await p.new_app("asr", asr_flow(p.voices, language=None, prompt=""))
        work = p.settings.media_dir / "_work" / "j_1"
        work.mkdir(parents=True)
        (work / "成片.mp4").write_bytes(b"\x00" * 10)
        voice = await p.add(p.voices, "配音.wav")
        for value in (
            {"aep_file": {"path": "_work/j_1/成片.mp4", "name": "成片.mp4"}},
            voice.rel_path,
            {"path": voice.rel_path, "name": "配音.wav"},
        ):
            resp = await p.api.post(
                "/apps/asr/nodes/asr/test", json={"values": {"start.voiceover": value}}
            )
            tried = resp.json()
            assert tried["status"] == "done", tried
            assert tried["outputs"]["segments"] == HEARD["segments"]
        bodies = transcriptions(p)
        assert [b["file"]["filename"] for b in bodies] == ["成片.mp4", "配音.wav", "配音.wav"]
        assert all("language" not in b and "prompt" not in b for b in bodies)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (42, "要转写的不是文件：42"),
        ({"name": "x"}, "要转写的不是文件"),
        ("../x.m4a", "文件不在媒体库里：../x.m4a"),
        ("/etc/passwd", "文件不在媒体库里：/etc/passwd"),
        ("LINK", "文件不在媒体库里"),
        ("_work/none.m4a", "文件不见了：_work/none.m4a"),
        ("PICTURE", "只能转写音频或视频：图.png"),
        ("BIG", "文件超过 1 MB，转写模型一次只收这么大"),
    ],
)
async def test_what_a_transcribe_node_refuses(tmp_path, value, message):
    async with Media(tmp_path, transcribe_max_mb=1) as p:
        await p.new_app("asr", asr_flow(p.voices))
        voice = await p.add(p.voices, "配音.m4a")
        root = p.settings.media_dir
        if value == "LINK":
            (root / "_work").mkdir(parents=True)
            (root / "_work" / "link.m4a").symlink_to(root / voice.rel_path)
            value = "_work/link.m4a"
        elif value == "PICTURE":
            value = (await p.add(p.voices, "图.png")).rel_path
        elif value == "BIG":
            big = await p.add(p.voices, "长.m4a")
            (root / big.rel_path).write_bytes(b"\x00" * (1024 * 1024 + 1))
            value = big.rel_path
        resp = await p.api.post(
            "/apps/asr/nodes/asr/test", json={"values": {"start.voiceover": value}}
        )
        tried = resp.json()
        assert tried["status"] == "error"
        assert tried["error"]["type"] == "input_error"
        assert message in tried["error"]["message"]
        assert transcriptions(p) == []


async def test_a_model_failure_takes_the_error_branch(tmp_path):
    async with Media(tmp_path) as p:
        on_error = {"then": "branch"}
        config = asr_flow(p.voices, on_error=on_error)
        nodes, edges = config["graph"]["nodes"], config["graph"]["edges"]
        nodes.append({"id": "sorry", "type": "template", "title": "抱歉", "template": "没听清"})
        nodes[-2]["outputs"] = [{"key": "heard", "from": ["{{asr.text}}", "{{sorry.text}}"]}]
        edges += [{"from": "asr", "branch": "error", "to": "sorry"}, {"from": "sorry", "to": "end"}]
        await p.new_app("asr", config)
        voice = await p.add(p.voices, "配音.m4a")
        p.gateway.status = 500
        run = await p.run("asr", {"voiceover": voice.item_id})
        assert run["status"] == "done", run["error"]
        assert run["result"]["heard"] == "没听清"
        p.gateway.transcript = {"segments": "坏了"}
        p.gateway.status = 200
        resp = await p.api.post(
            "/apps/asr/nodes/asr/test", json={"values": {"start.voiceover": voice.rel_path}}
        )
        assert resp.json()["outputs"] == {
            "text": "",
            "language": "zh",
            "duration_seconds": None,
            "segments": [],
            "words": [],
        }
        p.gateway.transcript = []  # type: ignore[assignment]
        resp = await p.api.post(
            "/apps/asr/nodes/asr/test", json={"values": {"start.voiceover": voice.rel_path}}
        )
        error = resp.json()["error"]
        assert error["type"] == "model_error" and "转写模型的回答格式不对" in error["message"]


async def test_settings_are_checked_when_saved(tmp_path):
    async with Media(tmp_path) as p:
        resp = await p.api.post("/apps", json={"app": "asr", "title": "转写"})
        assert resp.status_code == 201, resp.text
        bad = asr_flow(p.voices, model="", audio=" ", language="中文", prompt="x" * 501)
        resp = await p.api.put("/apps/asr/draft", json={"config": bad})
        assert resp.status_code == 422, resp.text
        found = {tuple(d["loc"][5:]): d["msg"] for d in resp.json()["detail"]}
        assert found == {
            ("model",): "请选择转写模型",
            ("audio",): "请选择要转写的文件",
            ("language",): "语言要写 2–3 个小写字母，比如 zh、en",
            ("prompt",): "提示最多 500 个字符",
        }
        unknown = asr_flow(p.voices, model="nope")
        resp = await p.api.put("/apps/asr/draft", json={"config": unknown})
        assert resp.json()["detail"][0]["msg"] == "模型网关上没有 nope"
        types = (await p.api.get("/node-types")).json()
        [spec] = [t for t in types if t["type"] == "transcribe"]
        assert spec["defaults"] == {"model": "transcribe", "audio": "", "language": "zh",
                                    "prompt": ""}  # fmt: skip
        assert [o["key"] for o in spec["outputs"]] == [
            "text",
            "language",
            "duration_seconds",
            "segments",
            "words",
        ]


# The media kind of the data outbound policy


def test_the_media_kind_of_the_policy():
    assert platform_default(Settings()).media == ("audio", "image")
    assert platform_default(Settings(data_media="image, x")).media == ("image",)
    assert platform_default(Settings(data_media="")).media == ()
    # Runs from before the media kind existed sent no media: any is allowed.
    assert DataPolicy.from_dict({"samples": "full", "models": None}).media == ("audio", "image")
    assert DataPolicy.from_dict({"media": ["image"]}).media == ("image",)
    assert DataPolicy(media=("image",)).as_dict()["media"] == ["image"]
    platform = DataPolicy(media=("audio",))
    assert effective_policy({"policy": {"media": None}}, platform).media == ("audio",)
    assert effective_policy({"policy": {"media": []}}, platform).media == ()
    assert effective_policy({"policy": {"media": ["image"]}}, platform).media == ("image",)
    assert effective_policy(None, platform).media == ("audio",)
    assert media_problem(DataPolicy(media=()), "audio", "转写配音") == (
        "平台不允许把音频发给模型，节点「转写配音」要发"
    )
    assert media_problem(DataPolicy(media=()), "image", "看画面", "教练") == (
        "项目「教练」不允许把图片发给模型，节点「看画面」要发"
    )
    assert media_problem(DataPolicy(), "audio", "x") is None


async def test_a_run_whose_policy_keeps_audio_in_sends_nothing(tmp_path):
    async with Media(tmp_path, data_media="image") as p:
        await p.new_app("asr", asr_flow(p.voices))
        voice = await p.add(p.voices, "配音.m4a")
        run = await p.run("asr", {"voiceover": voice.item_id})
        assert run["status"] == "error"
        assert run["error"] == (
            "节点「转写配音」(asr)：平台不允许把音频发给模型，节点「转写配音」要发"
        )
        assert transcriptions(p) == []
        assert run["policy"]["media"] == ["image"]


async def test_a_model_the_policy_does_not_allow_is_refused_at_the_start(tmp_path):
    async with Media(tmp_path, allowed_models="qwen") as p:
        await p.new_app("asr", asr_flow(p.voices, model="mock"))
        voice = await p.add(p.voices, "配音.m4a")
        resp = await p.api.post("/runs", json={"app": "asr", "input": {"voiceover": voice.item_id}})
        assert resp.status_code == 422
        assert "节点「转写配音」用的是 mock" in resp.json()["detail"]


def test_the_silence_a_model_test_sends_is_a_wav() -> None:
    from aep.models import SILENCE

    assert SILENCE[:4] == b"RIFF" and SILENCE[8:12] == b"WAVE"
    assert len(SILENCE) == 44 + 32000


def test_a_transcript_is_tidied() -> None:
    nested = {
        "text": "",
        "segments": [
            {"start": 1, "end": 0.5, "text": " 第二句", "words": [{"start": 1, "end": 1.2,
                                                                 "word": "第二"}]},
            {"start": 0, "end": 0.9, "text": "第一句 "},
            {"start": -1, "end": 2, "text": "坏的时间"},
            {"start": True, "end": 2, "text": "布尔"},
        ],
    }  # fmt: skip
    found = llm.transcript(nested, "zh")
    assert found.text == "第一句第二句"
    assert found.segments == [
        {"start": 0.0, "end": 0.9, "text": "第一句"},
        {"start": 1.0, "end": 1.0, "text": "第二句"},
    ]
    assert found.words == [{"start": 1.0, "end": 1.2, "word": "第二"}]
    assert (found.language, found.duration_seconds) == ("zh", 1.0)
    with pytest.raises(ValueError, match="转写模型的回答格式不对"):
        llm.transcript([], None)


TRANSCRIBE_MODEL = {
    "model_name": "transcribe",
    "litellm_params": {
        "model": "openai/Systran/faster-whisper-small",
        "api_base": "http://asr:8000/v1",
    },
    "model_info": {
        "id": "cfg-transcribe",
        "db_model": False,
        "aep_display_name": "语音转写（faster-whisper）",
        "aep_purpose": "transcription",
        "aep_provider": "openai_compatible",
        "mode": "audio_transcription",
    },
}


async def test_the_model_page_lists_and_tests_a_transcription_model():
    async with Platform() as p:
        p.fake.deployments.append(TRANSCRIBE_MODEL)
        # A model that says only its mode is a transcription model too.
        p.fake.deployments.append(
            {
                "model_name": "whisper",
                "litellm_params": {"model": "openai/whisper-1"},
                "model_info": {"id": "cfg-whisper", "db_model": False,
                               "mode": "audio_transcription"},
            }
        )  # fmt: skip
        listing = (await p.api.get("/models")).json()
        tested = (await p.api.post("/models/transcribe/test")).json()
        made = await p.api.post(
            "/models",
            json={
                "alias": "asr2",
                "display_name": "转写",
                "purpose": "transcription",
                "provider": "openai_compatible",
                "model": "whisper-1",
            },
        )
    assert [m["alias"] for m in listing["models"]][-2:] == ["transcribe", "whisper"]
    found = next(m for m in listing["models"] if m["alias"] == "transcribe")
    assert (found["purpose"], found["display_name"]) == (
        "transcription",
        "语音转写（faster-whisper）",
    )
    assert next(m for m in listing["models"] if m["alias"] == "whisper")["purpose"] == (
        "transcription"
    )
    # App pages offer it, by purpose, to the transcribe node.
    assert {"alias": "transcribe", "label": "语音转写（faster-whisper）",
            "purpose": "transcription", "allowed": True,
            "vision": False} in await p.models.choices()  # fmt: skip
    assert tested["ok"] and tested["reply"] is None
    [sent] = [r for r in p.gateway.requests if r["url"].endswith("/audio/transcriptions")]
    assert sent["body"]["file"] == {"filename": "测试.wav", "size": 44 + 32000}
    assert made.status_code == 422


async def test_a_project_sets_which_media_its_runs_may_send(tmp_path):
    async with Media(tmp_path) as p:
        options = (await p.api.get("/projects/options")).json()
        assert options["media_kinds"] == [
            {"value": "audio", "label": "音频"},
            {"value": "image", "label": "图片"},
        ]
        assert options["defaults"]["media"] == ["audio", "image"]
        bad = await p.api.post("/projects", json=project(policy={"media": ["video"]}))
        assert bad.status_code == 422
        twice = await p.api.post("/projects", json=project(policy={"media": ["audio", "audio"]}))
        assert twice.status_code == 422
        made = await p.api.post("/projects", json=project(policy={"media": ["image"]}))
        assert made.status_code == 201, made.text
        assert made.json()["policy"]["media"] == ["image"]

        flow = asr_flow(p.voices)
        flow["graph"]["nodes"][0]["project"] = "required"
        await p.new_app("asr", flow)
        voice = await p.add(p.voices, "配音.m4a")
        resp = await p.api.post(
            "/runs",
            json={
                "app": "asr",
                "input": {"voiceover": voice.item_id},
                "project": {"project_id": "jade-ibf"},
            },
        )
        assert resp.status_code == 201, resp.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert run["policy"]["media"] == ["image"]
        assert run["project"]["policy"]["media"] == ["image"]
        assert run["error"] == (
            "节点「转写配音」(asr)：项目「IBF 入库运费」不允许把音频发给模型，节点「转写配音」要发"
        )
        assert transcriptions(p) == []
