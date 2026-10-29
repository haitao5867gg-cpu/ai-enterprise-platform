"""The retrieval contract, shared by HTTP requests and saved flow nodes."""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator


class Rerank(BaseModel):
    model: str = Field(min_length=1, strict=True)
    candidates: int = Field(20, ge=5, le=50, strict=True)

    @field_validator("model")
    @classmethod
    def available_model(cls, value: str, info: ValidationInfo) -> str:
        options = (info.context or {}).get("rerank_models")
        if options is not None and value not in {option["model"] for option in options}:
            raise ValueError(f"没有可用的重排模型别名「{value}」")
        return value

    @field_validator("candidates")
    @classmethod
    def enough_candidates(cls, value: int, info: ValidationInfo) -> int:
        if value < (info.context or {}).get("top_k", 5):
            raise ValueError("重排候选数不能少于 top_k")
        return value


class Retrieval(BaseModel):
    mode: Literal["vector", "keyword", "hybrid"] = "vector"
    vector_weight: float = Field(0.7, ge=0, le=1, strict=True)
    rerank: Rerank | None = None


def defaults() -> dict[str, Any]:
    """New pages/nodes use hybrid; omitted stored settings still use vector."""
    return Retrieval(mode="hybrid").model_dump()
