"""Request validation errors in Chinese, for APIs whose pages show ``detail`` as it is.

A router built with ``route_class=chinese_errors(...)`` answers a body that does not
validate with 422 and one sentence: the field's label from ``labels`` (else its
name) and what is wrong, or the validator's own message. Models that inherit
``NoNulls`` refuse explicit nulls (docs/api/knowledge.md, docs/api/datasources.md).
"""

from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, model_validator


def chinese_errors(labels: Mapping[str, str] | None = None) -> type[APIRoute]:
    """A route class whose validation errors are Chinese; ``labels`` name the fields."""
    names = dict(labels or {})

    class ChineseErrors(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            handler = super().get_route_handler()

            async def handle(request: Request) -> Response:
                try:
                    return await handler(request)
                except RequestValidationError as exc:
                    error = exc.errors()[0]
                    field = ".".join(str(part) for part in error["loc"][1:]) or "请求"
                    if field in names:
                        detail = f"「{names[field]}」的格式、长度或范围不正确"
                    else:
                        detail = f"参数「{field}」的格式、长度或范围不正确"
                    if error["type"] == "value_error" and (
                        cause := error.get("ctx", {}).get("error")
                    ):
                        detail = str(cause)
                    raise HTTPException(422, detail) from exc

            return handle

    return ChineseErrors


class NoNulls(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def no_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(v is None for v in value.values()):
            raise ValueError("请求字段不能为 null")
        return value
