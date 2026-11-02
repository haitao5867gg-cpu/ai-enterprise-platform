"""Models API: the gateway's model aliases, adding Claude and other providers from the
page, testing them, platform default models and usage. Contract: docs/api/models.md."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints

from aep.api.validation import NoNulls, chinese_errors
from aep.models import ModelError, ModelService

router = APIRouter(
    prefix="/models",
    tags=["models"],
    route_class=chinese_errors(
        {
            "alias": "别名",
            "display_name": "显示名",
            "purpose": "用途",
            "provider": "供应商",
            "model": "型号",
            "api_base": "地址",
            "api_version": "API 版本",
            "aws_region": "区域",
            "key": "key",
            "description": "说明",
            "chat": "默认对话模型",
            "embedding": "默认向量模型",
        }
    ),
)

NO_NUL = r"^[^\x00]*$"
DisplayName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60, pattern=NO_NUL)
]
Description = Annotated[str, StringConstraints(max_length=500, pattern=NO_NUL)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2048, pattern=NO_NUL)]


def get_models(request: Request) -> ModelService:
    return request.app.state.models


Models = Annotated[ModelService, Depends(get_models)]


class CreateModel(NoNulls):
    alias: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    display_name: DisplayName
    purpose: Literal["chat", "embedding", "rerank"]
    provider: Annotated[str, StringConstraints(max_length=40)]
    model: Short
    api_base: Short | None = None
    api_version: Short | None = None
    aws_region: Short | None = None
    key: dict[str, Any] | None = None
    description: Description = ""


class UpdateModel(NoNulls):
    model_config = {"extra": "allow"}

    display_name: DisplayName | None = None
    description: Description | None = None
    model: Short | None = None
    api_base: Short | None = None
    api_version: Short | None = None
    aws_region: Short | None = None
    key: dict[str, Any] | None = None


class Defaults(NoNulls):
    chat: str | None = Field(None, max_length=40)
    embedding: str | None = Field(None, max_length=40)


def _fail(exc: ModelError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


@router.get("")
async def list_models(models: Models) -> dict[str, Any]:
    return await models.listing()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_model(body: CreateModel, models: Models) -> dict[str, Any]:
    try:
        return await models.create(body.model_dump(exclude_none=True))
    except ModelError as exc:
        raise _fail(exc) from exc


@router.put("/defaults")
async def set_defaults(body: Defaults, models: Models) -> dict[str, str]:
    try:
        return await models.set_defaults(body.chat, body.embedding)
    except ModelError as exc:
        raise _fail(exc) from exc


@router.get("/{alias}")
async def get_model(alias: str, models: Models) -> dict[str, Any]:
    try:
        return await models.get(alias)
    except ModelError as exc:
        raise _fail(exc) from exc


@router.patch("/{alias}")
async def update_model(alias: str, body: UpdateModel, models: Models) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    try:
        return await models.update(alias, changes)
    except ModelError as exc:
        raise _fail(exc) from exc


@router.delete("/{alias}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(alias: str, models: Models) -> Response:
    try:
        await models.delete(alias)
    except ModelError as exc:
        raise _fail(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{alias}/test")
async def test_model(alias: str, models: Models) -> dict[str, Any]:
    try:
        return await models.test(alias)
    except ModelError as exc:
        raise _fail(exc) from exc


class UsageApp(BaseModel):
    app: str
    title: str
    version: int
    nodes: list[str]
    via: Literal["literal", "default"]


@router.get("/{alias}/usage")
async def model_usage(alias: str, models: Models) -> dict[str, list[UsageApp]]:
    try:
        await models.get(alias)
    except ModelError as exc:
        raise _fail(exc) from exc
    return {"apps": [UsageApp(**use) for use in await models.usage(alias)]}
