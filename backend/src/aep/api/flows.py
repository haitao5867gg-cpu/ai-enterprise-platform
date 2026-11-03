"""Node types and app templates, for building flow apps (docs/api/flow-apps.md)."""

from typing import Any

from fastapi import APIRouter, Request

from aep.i18n import request_language
from aep.messages_en import NODE_TYPES
from aep.runtime.flow.builtin import TEMPLATES
from aep.runtime.flow.nodes import TYPES

router = APIRouter(tags=["apps"])


def _english(described: dict[str, Any]) -> dict[str, Any]:
    """A node type's names and descriptions in English; its settings stay as they are."""

    def word(text: str) -> str:
        return NODE_TYPES.get(text, text)

    return {
        **described,
        "label": word(described["label"]),
        "description": word(described["description"]),
        "outputs": [
            {**output, "description": word(output["description"])}
            for output in described["outputs"]
        ],
        "presets": [
            {**preset, "label": word(preset["label"]), "description": word(preset["description"])}
            for preset in described["presets"]
        ],
    }


@router.get("/node-types")
async def node_types(request: Request) -> list[dict[str, Any]]:
    described = [t.describe() for t in TYPES.values()]
    if request_language(request) == "en":
        return [_english(item) for item in described]
    return described


@router.get("/app-templates")
async def app_templates() -> list[dict[str, Any]]:
    return [t.describe() for t in TEMPLATES.values()]
