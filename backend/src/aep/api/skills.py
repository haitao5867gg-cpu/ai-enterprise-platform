"""Skills API: list, read, edit and publish skills. Contract: docs/api/apps-and-skills.md."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from aep.runtime.catalog import AppCatalog
from aep.skills import (
    NoDraft,
    Skill,
    SkillContent,
    SkillError,
    SkillExists,
    SkillNotFound,
    SkillStore,
)

router = APIRouter(prefix="/skills", tags=["skills"])


def get_skills(request: Request) -> SkillStore:
    return request.app.state.skills


def get_catalog(request: Request) -> AppCatalog:
    return request.app.state.catalog


Skills = Annotated[SkillStore, Depends(get_skills)]
Catalog = Annotated[AppCatalog, Depends(get_catalog)]


class SkillDraft(BaseModel):
    description: str
    body: str
    references: dict[str, str] = {}
    actor: str | None = None


class CreateSkill(SkillDraft):
    name: str


class Publish(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    actor: str | None = None


def _content(c: SkillContent) -> dict[str, Any]:
    return {
        "version": c.version,
        "description": c.description,
        "body": c.body,
        "references": c.references,
        "note": c.note,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat(),
    }


def _summary(s: Skill, used_by: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "name": s.name,
        "description": s.description,
        "published_version": s.published_version,
        "has_draft": s.draft is not None,
        "updated_at": s.updated_at.isoformat(),
        "used_by": used_by.get(s.name, []),
    }


async def _detail(s: Skill, catalog: AppCatalog) -> dict[str, Any]:
    return _summary(s, await catalog.used_by()) | {
        "draft": _content(s.draft) if s.draft else None,
        "versions": [
            {
                "version": v.version,
                "note": v.note,
                "created_by": v.created_by,
                "created_at": v.created_at.isoformat(),
            }
            for v in s.versions
        ],
        "current": _content(s.current) if s.current else None,
    }


def _invalid(exc: SkillError) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": ["body", *field.split(".", 1)], "msg": message, "type": "value_error"}
            for field, message in exc.problems
        ]
    )


def _draft(body: SkillDraft) -> SkillContent:
    return SkillContent(body.description, body.body, body.references, created_by=body.actor)


@router.get("")
async def list_skills(skills: Skills, catalog: Catalog) -> list[dict[str, Any]]:
    used_by = await catalog.used_by()
    return [_summary(s, used_by) for s in await skills.all()]


@router.get("/{name}")
async def get_skill(name: str, skills: Skills, catalog: Catalog) -> dict[str, Any]:
    found = await skills.get(name)
    if found is None:
        raise HTTPException(404, f"没有 Skill {name}")
    return await _detail(found, catalog)


@router.get("/{name}/versions/{version}")
async def get_version(name: str, version: int, skills: Skills) -> dict[str, Any]:
    found = await skills.version(name, version)
    if found is None:
        raise HTTPException(404, f"Skill {name} 没有版本 {version}")
    return _content(found)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(body: CreateSkill, skills: Skills, catalog: Catalog) -> dict[str, Any]:
    try:
        created = await skills.create(body.name, _draft(body))
    except SkillError as exc:
        raise _invalid(exc) from exc
    except SkillExists as exc:
        raise HTTPException(409, f"已经有叫 {body.name} 的 Skill") from exc
    return await _detail(created, catalog)


@router.put("/{name}/draft")
async def save_draft(
    name: str, body: SkillDraft, skills: Skills, catalog: Catalog
) -> dict[str, Any]:
    try:
        saved = await skills.save_draft(name, _draft(body))
    except SkillError as exc:
        raise _invalid(exc) from exc
    except SkillNotFound as exc:
        raise HTTPException(404, f"没有 Skill {name}") from exc
    return await _detail(saved, catalog)


@router.delete("/{name}/draft", status_code=status.HTTP_204_NO_CONTENT)
async def discard_draft(name: str, skills: Skills) -> Response:
    try:
        await skills.discard_draft(name)
    except NoDraft as exc:
        raise HTTPException(404, f"Skill {name} 没有草稿") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/publish")
async def publish(name: str, body: Publish, skills: Skills, catalog: Catalog) -> dict[str, Any]:
    if not body.note.strip():
        raise HTTPException(422, "发布说明不能为空")
    if await skills.get(name) is None:
        raise HTTPException(404, f"没有 Skill {name}")
    try:
        published = await skills.publish(name, body.note.strip(), body.actor)
    except NoDraft as exc:
        raise HTTPException(409, f"Skill {name} 没有草稿可以发布") from exc
    return await _detail(published, catalog)
