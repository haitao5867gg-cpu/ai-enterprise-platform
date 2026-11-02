"""Asset catalog API (docs/api/catalog.md): every platform asset in one list, its owner and
tags, who uses it, its quality, and proposing it to the assets repository."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from aep.asset_catalog import KINDS, AssetCatalog, NoRepo, NotFound, Refused
from aep.assets import AssetRepoError
from aep.auth import who

router = APIRouter(prefix="/catalog", tags=["catalog"])


def get_catalog(request: Request) -> AssetCatalog:
    return request.app.state.asset_catalog


Catalog = Annotated[AssetCatalog, Depends(get_catalog)]
NOT_FOUND = "没有这项资产"


class Meta(BaseModel):
    owner: str | None = Field(default=None, max_length=50)
    tags: list[Annotated[str, Field(max_length=20)]] = Field(default_factory=list, max_length=10)
    # Who changes it. Until sign-in is on the client says so (as for runs).
    actor: str | None = Field(default=None, max_length=100)


class Propose(BaseModel):
    note: str = Field(default="", max_length=500)
    actor: str | None = Field(default=None, max_length=100)


@router.get("")
async def list_assets(
    catalog: Catalog, kind: str | None = None, q: str | None = None
) -> dict[str, Any]:
    if kind is not None and kind not in KINDS:
        raise HTTPException(422, f"不认识的类别 {kind}")
    return {"items": await catalog.items(kind, q), "repo_ready": catalog.repo_ready}


@router.get("/{kind}/{asset_id}")
async def get_asset(kind: str, asset_id: str, catalog: Catalog) -> dict[str, Any]:
    try:
        return await catalog.item(kind, asset_id)
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None


@router.put("/{kind}/{asset_id}")
async def set_meta(
    kind: str, asset_id: str, body: Meta, catalog: Catalog, request: Request
) -> dict[str, Any]:
    try:
        return await catalog.set_meta(
            kind, asset_id, body.owner, body.tags, who(request, body.actor)
        )
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/{kind}/{asset_id}/pull-request", status_code=status.HTTP_201_CREATED)
async def propose(
    kind: str, asset_id: str, catalog: Catalog, request: Request, body: Propose | None = None
) -> dict[str, Any]:
    body = body or Propose()
    try:
        return await catalog.propose(kind, asset_id, body.note, who(request, body.actor))
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except Refused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except NoRepo:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "没配资产仓库（AEP_ASSETS_REPO、AEP_ASSETS_GIT_URL）"
        ) from None
    except AssetRepoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
