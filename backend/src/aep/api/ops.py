"""The operations dashboard and per-app model budgets (docs/api/ops.md)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from aep.api.validation import NoNulls, chinese_errors
from aep.auth import who
from aep.ops import DAYS, MAX_BUDGET, MAX_PRICE, Ops

router = APIRouter(tags=["ops"], route_class=chinese_errors())


def get_ops(request: Request) -> Ops:
    return request.app.state.ops


OpsDep = Annotated[Ops, Depends(get_ops)]


class PriceBody(BaseModel):  # null clears the price
    compute_hourly_usd: float | None = Field(None, ge=0, le=MAX_PRICE)


class BudgetBody(NoNulls):
    monthly_usd: float = Field(ge=0.01, le=MAX_BUDGET)
    warn_at: float = Field(0.8, ge=0.5, le=1)
    block: bool = True
    actor: str | None = Field(None, max_length=50)


@router.get("/ops/summary")
async def summary(ops: OpsDep, days: Annotated[int, Query()] = 30) -> dict[str, Any]:
    if days not in DAYS:
        raise HTTPException(422, "days 只能是 7、30 或 90")
    return await ops.summary(days)


@router.put("/ops/settings")
async def set_settings(body: PriceBody, ops: OpsDep) -> dict[str, Any]:
    return {"compute_hourly_usd": await ops.set_price(body.compute_hourly_usd)}


async def _app(ops: Ops, app: str) -> None:
    if app not in await ops.titles():
        raise HTTPException(404, f"没有应用「{app}」")


@router.get("/apps/{app}/budget")
async def get_budget(app: str, ops: OpsDep) -> dict[str, Any] | None:
    await _app(ops, app)
    return await ops.budget(app)


@router.put("/apps/{app}/budget")
async def put_budget(
    app: str, body: BudgetBody, ops: OpsDep, request: Request
) -> dict[str, Any] | None:
    await _app(ops, app)
    return await ops.set_budget(
        app, body.monthly_usd, body.warn_at, body.block, who(request, body.actor)
    )


@router.delete("/apps/{app}/budget", status_code=204)
async def delete_budget(app: str, ops: OpsDep) -> Response:
    await _app(ops, app)
    await ops.delete_budget(app)
    return Response(status_code=204)
