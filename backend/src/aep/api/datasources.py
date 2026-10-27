"""Data sources API: register connectors and see what tools they offer."""

import shutil
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from aep.config import Settings, get_settings
from aep.datasources import DataSource, DataSourceKind, DataSourceStore, DuplicateName
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError
from aep.uploads import MAX_FILES, UploadError, stage_dataset

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


class FileDataSourceOut(BaseModel):
    datasource: DataSourceOut
    dataset_id: str
    schema_name: str = Field(alias="schema")
    tables: list[dict[str, Any]]


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


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def upload_files(
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
    name: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> FileDataSourceOut:
    name = name.strip()
    if not name or len(name) > 100:
        raise HTTPException(422, "数据集名称必须为 1～100 个字符")
    if not files:
        raise HTTPException(422, "请选择至少一个 CSV 或 XLSX 文件")
    if len(files) > MAX_FILES:
        raise HTTPException(422, "单次最多上传 20 个文件")
    try:
        staged = await run_in_threadpool(stage_dataset, files, settings)
    except UploadError as exc:
        raise HTTPException(422, str(exc)) from exc
    final = settings.uploads_dir / staged.dataset_id
    registered = False
    try:
        staged.path.rename(final)
        ds = await store.create(
            name,
            "file",
            f"{settings.files_connector_url.rstrip('/')}/datasets/{staged.dataset_id}/mcp",
            "上传文件：" + "、".join(t["table"] for t in staged.tables),
        )
        registered = True
    except DuplicateName as exc:
        raise HTTPException(409, f"已存在名为「{name}」的数据源") from exc
    finally:
        if not registered:
            await run_in_threadpool(shutil.rmtree, final if final.exists() else staged.path)
    return FileDataSourceOut(
        datasource=_out(ds),
        dataset_id=staged.dataset_id,
        schema=staged.schema,
        tables=staged.tables,
    )


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
