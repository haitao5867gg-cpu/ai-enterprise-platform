"""Data sources API: register connectors and see what tools they offer."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from aep.datasources import DataSource, DataSourceKind, DataSourceStore, DuplicateName
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError

router = APIRouter(prefix="/datasources", tags=["datasources"])


def get_store(request: Request) -> DataSourceStore:
    return request.app.state.datasources


def get_tools(request: Request) -> McpTools:
    return request.app.state.mcp_tools


Store = Annotated[DataSourceStore, Depends(get_store)]
Tools = Annotated[McpTools, Depends(get_tools)]


class CreateDataSource(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: DataSourceKind
    connector_url: str = Field(pattern=r"^https?://", description="The connector's MCP endpoint")
    description: str = ""


class DataSourceOut(BaseModel):
    datasource_id: str
    name: str
    kind: DataSourceKind
    connector_url: str
    description: str
    created_at: str


class ToolOut(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


def _out(d: DataSource) -> DataSourceOut:
    return DataSourceOut(
        datasource_id=d.datasource_id,
        name=d.name,
        kind=d.kind,
        connector_url=d.connector_url,
        description=d.description,
        created_at=d.created_at.isoformat(),
    )


async def _get(store: DataSourceStore, datasource_id: str) -> DataSource:
    ds = await store.get(datasource_id)
    if ds is None:
        raise HTTPException(404, "data source not found")
    return ds


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_datasource(body: CreateDataSource, store: Store) -> DataSourceOut:
    try:
        ds = await store.create(body.name, body.kind, body.connector_url, body.description)
    except DuplicateName as exc:
        raise HTTPException(409, f"a data source named {body.name!r} already exists") from exc
    return _out(ds)


@router.get("")
async def list_datasources(store: Store) -> list[DataSourceOut]:
    return [_out(d) for d in await store.all()]


@router.get("/{datasource_id}")
async def get_datasource(datasource_id: str, store: Store) -> DataSourceOut:
    return _out(await _get(store, datasource_id))


@router.get("/{datasource_id}/tools")
async def list_datasource_tools(datasource_id: str, store: Store, tools: Tools) -> list[ToolOut]:
    """The connector's tools; also a connectivity check."""
    ds = await _get(store, datasource_id)
    try:
        listed = await tools.list_tools(ds.connector_url)
    except ConnectorUnavailable as exc:
        raise HTTPException(502, f"connector unreachable: {exc}") from exc
    except ToolError as exc:
        raise HTTPException(502, f"connector error: {exc}") from exc
    return [
        ToolOut(name=t.name, description=t.description, input_schema=t.input_schema) for t in listed
    ]
