"""Assets API: the index of bundles the platform opened as PRs in the assets repository."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from aep.assets import Asset, AssetStatus, AssetStore

router = APIRouter(prefix="/assets", tags=["assets"])


def get_store(request: Request) -> AssetStore:
    return request.app.state.asset_store


Store = Annotated[AssetStore, Depends(get_store)]


class AssetOut(BaseModel):
    name: str
    version: int
    status: AssetStatus
    run_id: str
    pr_number: int
    pr_url: str
    branch: str
    commit: str
    created_at: str
    updated_at: str


def _out(a: Asset) -> AssetOut:
    return AssetOut(
        name=a.name,
        version=a.version,
        status=a.status,
        run_id=a.run_id,
        pr_number=a.pr_number,
        pr_url=a.pr_url,
        branch=a.branch,
        commit=a.commit_sha,
        created_at=a.created_at.isoformat(),
        updated_at=a.updated_at.isoformat(),
    )


@router.get("")
async def list_assets(store: Store, name: str | None = None) -> list[AssetOut]:
    """Every version, by name and newest version first; ``name`` keeps one bundle's versions."""
    return [_out(a) for a in await store.find(name)]
