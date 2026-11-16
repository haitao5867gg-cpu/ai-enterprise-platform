"""Where a media item came from (docs/api/media.md, ``source``): the rules a person's
change follows, and a tool's file reference naming the stock site it downloaded from
(docs/api/media-processing.md, "产出的文件存进媒体库")."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, field_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Link = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["upload", "stock", "run"]
    site: Text | None = None
    url: Link | None = None
    author: Text | None = None
    license: Text | None = None
    license_url: Link | None = None
    run_id: Text | None = None

    @field_validator("url", "license_url")
    @classmethod
    def web_address(cls, value: str | None) -> str | None:
        # Pages show these as links; only web addresses.
        if value and not value.lower().startswith(("https://", "http://")):
            raise ValueError("来源链接要以 http:// 或 https:// 开头")
        return value or None


def stock_source(value: Any) -> dict[str, Any] | None:
    """A file reference's ``source`` when it names a stock site and follows the rules
    above; anything else is not one (the file is then recorded as the run's)."""
    if not isinstance(value, dict) or value.get("kind") != "stock":
        return None
    try:
        return Source.model_validate(value).model_dump()
    except ValidationError:
        return None
