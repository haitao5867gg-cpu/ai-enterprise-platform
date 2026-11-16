"""The ``transcribe`` node (语音转写): sends an audio or video file on the media volume to a
transcription model through the gateway and puts out the text with the time of each
sentence and word (docs/api/transcription.md)."""

import asyncio
import re
from typing import Any

import openai

from aep import llm
from aep.media.files import KINDS, VolumeFileError, media_path, volume_file
from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    INPUT_ERROR,
    CheckEnv,
    NodeFailure,
    NodeType,
    Outcome,
    Problems,
    Runtime,
)

MAX_PROMPT = 500
LANGUAGE = re.compile(r"^[a-z]{2,3}$")
OUTPUTS = ("text", "language", "duration_seconds", "segments", "words")


async def check_transcribe(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    model = node.get("model")
    if not isinstance(model, str) or not model.strip():
        p.add(("model",), "请选择转写模型")
    elif env.strict and env.models is not None and model not in env.models:
        p.add(("model",), f"模型网关上没有 {model}")
    audio = node.get("audio")
    if not isinstance(audio, str) or not audio.strip():
        p.add(("audio",), "请选择要转写的文件")
    language = node.get("language")
    if language is not None and (not isinstance(language, str) or not LANGUAGE.match(language)):
        p.add(("language",), "语言要写 2–3 个小写字母，比如 zh、en")
    prompt = node.get("prompt", "")
    if not isinstance(prompt, str):
        p.add(("prompt",), "提示必须是文字")
    elif len(prompt) > MAX_PROMPT:
        p.add(("prompt",), f"提示最多 {MAX_PROMPT} 个字符")


async def run_transcribe(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    value = refs.resolve(node["audio"], rt.lookup(state))
    path = media_path(value)
    if path is None:
        raise NodeFailure(INPUT_ERROR, f"要转写的不是文件：{refs.as_text(value)[:200]}")
    try:
        file = await asyncio.to_thread(volume_file, rt.settings.media_dir, path)
    except VolumeFileError as exc:
        raise NodeFailure(INPUT_ERROR, str(exc)) from None
    if KINDS.get(file.suffix.lower()) not in ("audio", "video"):
        raise NodeFailure(INPUT_ERROR, f"只能转写音频或视频：{file.name}")
    limit = rt.settings.transcribe_max_mb
    if (await asyncio.to_thread(file.stat)).st_size > limit * 1024 * 1024:
        raise NodeFailure(INPUT_ERROR, f"文件超过 {limit} MB，转写模型一次只收这么大")
    model = node["model"]
    rt.model_check(node, state)(model)
    rt.media_check(node, state)("audio")
    prompt = refs.render(node.get("prompt") or "", rt.model_lookup(state)).strip()
    data = await asyncio.to_thread(file.read_bytes)
    try:
        heard = await llm.transcribe(
            rt.llm_client,
            (file.name, data),
            model,
            language=node.get("language") or None,
            prompt=prompt[:MAX_PROMPT] or None,
        )
    except (TimeoutError, openai.APITimeoutError) as exc:
        raise NodeFailure("timeout", "转写模型没有在规定时间内回答") from exc
    except Exception as exc:  # noqa: BLE001 - any gateway failure is the model's
        raise NodeFailure("model_error", f"转写失败：{exc}") from exc
    return Outcome(heard.as_dict())


TYPES: dict[str, NodeType] = {
    "transcribe": NodeType(
        "transcribe",
        "语音转写",
        "把一段音频或视频的声音转成文字，带每句和每个字的时间",
        "model",
        "model",
        check_transcribe,
        run_transcribe,
        lambda node: None,
        lambda node: list(OUTPUTS),
        outputs=(
            ("text", "全文"),
            ("language", "认出的语言"),
            ("duration_seconds", "音频长度（秒）"),
            ("segments", "每句的文字和起止时间"),
            ("words", "每个字或词的起止时间（模型不给时为空）"),
        ),
        defaults={"model": "transcribe", "audio": "", "language": "zh", "prompt": ""},
        error_handling=True,
    ),
}
