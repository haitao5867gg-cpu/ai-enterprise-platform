"""Node types and app templates, for building flow apps (docs/api/flow-apps.md)."""

from typing import Any

from fastapi import APIRouter

from aep.runtime.flow.builtin import TEMPLATES
from aep.runtime.flow.nodes import TYPES

router = APIRouter(tags=["apps"])


@router.get("/node-types")
async def node_types() -> list[dict[str, Any]]:
    return [t.describe() for t in TYPES.values()]


@router.get("/app-templates")
async def app_templates() -> list[dict[str, Any]]:
    return [t.describe() for t in TEMPLATES.values()]
