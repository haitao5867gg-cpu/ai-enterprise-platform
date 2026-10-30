"""Data sources and tool services API: test and register connectors, rename them, see
their health, their tools and which apps use them. Contract: docs/api/datasources.md."""

import shutil
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, StringConstraints, model_validator
from starlette.concurrency import run_in_threadpool

from aep.api.apps import Catalog
from aep.api.validation import NoNulls, chinese_errors
from aep.config import Settings, get_settings
from aep.connector_check import TOOLS_FAILED, ConnectorCheck, connection_problem, failed_detail
from aep.connector_urls import mask_url, same_url
from aep.datasources import DataSource, DataSourceKind, DataSourceStore, DuplicateName, canon_id
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError
from aep.uploads import MAX_FILES, UploadError, stage_dataset

router = APIRouter(
    prefix="/datasources",
    tags=["datasources"],
    route_class=chinese_errors(
        {"name": "名称", "kind": "类型", "connector_url": "连接器地址", "description": "说明"}
    ),
)

NOT_FOUND = "没有找到这个数据源或工具服务"
FILE_KIND = "上传文件的数据源请用「上传文件」登记，不用填地址"
FILE_URL = "上传文件的数据源不能修改地址"
KIND_FIXED = "类型登记后不能修改"


def get_store(request: Request) -> DataSourceStore:
    return request.app.state.datasources


def get_tools(request: Request) -> McpTools:
    return request.app.state.mcp_tools


def get_check(request: Request) -> ConnectorCheck:
    return request.app.state.connector_check


Store = Annotated[DataSourceStore, Depends(get_store)]
Tools = Annotated[McpTools, Depends(get_tools)]
Check = Annotated[ConnectorCheck, Depends(get_check)]
Config = Annotated[Settings, Depends(get_settings)]
# Postgres text cannot hold NUL, so it is a format error rather than a 500.
NO_NUL = r"^[^\x00]*$"
Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=NO_NUL)
]


def _taken(name: str) -> str:
    return f"已经有叫「{name}」的数据源或工具服务了"


class CreateDataSource(NoNulls):
    name: Name
    kind: DataSourceKind
    # Checked by the address rules, which answer in Chinese (aep.connector_urls).
    connector_url: str = Field(description="The connector's MCP endpoint")
    description: str = Field("", max_length=2000, pattern=NO_NUL)


class UpdateDataSource(NoNulls):
    name: Name | None = None
    description: str | None = Field(None, max_length=2000, pattern=NO_NUL)
    connector_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def kind_is_fixed(cls, value: Any) -> Any:
        if isinstance(value, dict) and "kind" in value:
            raise ValueError(KIND_FIXED)
        return value


class TestConnector(NoNulls):
    kind: DataSourceKind
    connector_url: str


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
    read_only: bool


class ToolBrief(BaseModel):
    name: str
    description: str
    read_only: bool


class ProblemOut(BaseModel):
    code: str
    message: str


class TestOut(BaseModel):
    ok: bool
    latency_ms: int | None
    tools: list[ToolBrief]
    missing_tools: list[str]
    problems: list[ProblemOut]


class HealthOut(BaseModel):
    ok: bool
    latency_ms: int | None
    tool_count: int | None
    error: Literal["timeout", "refused", "not_mcp", "other"] | None
    message: str | None
    checked_at: str


class UsageApp(BaseModel):
    app: str
    title: str
    version: int
    via: Literal["literal", "default_input"]


class UsageOut(BaseModel):
    apps: list[UsageApp]


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
        connector_url=mask_url(d.connector_url),
        description=d.description,
        created_at=d.created_at.isoformat(),
    )


async def _get(store: DataSourceStore, datasource_id: str) -> DataSource:
    ds = await store.get(datasource_id)
    if ds is None:
        raise HTTPException(404, NOT_FOUND)
    return ds


async def _name_taken(store: DataSourceStore, name: str) -> bool:
    return any(d.name == name for d in await store.all())


@router.post("/test")
async def test_connector(
    body: TestConnector, tools: Tools, check: Check, settings: Config
) -> TestOut:
    """The address rules, then the connector's tools; a failed test is a 200 too."""
    if body.kind == "file":
        raise HTTPException(422, FILE_KIND)
    result = await check.test(tools, body.kind, body.connector_url, settings)
    return TestOut(
        ok=result.ok,
        latency_ms=result.latency_ms,
        tools=[
            ToolBrief(name=t.name, description=t.description, read_only=t.read_only)
            for t in result.tools
        ],
        missing_tools=result.missing_tools,
        problems=[ProblemOut(code=p.code, message=p.message) for p in result.problems],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_datasource(
    body: CreateDataSource, store: Store, tools: Tools, check: Check, settings: Config
) -> DataSourceOut:
    """Registered only after its test passes, with the normalised address."""
    if body.kind == "file":
        raise HTTPException(422, FILE_KIND)
    if await _name_taken(store, body.name):
        raise HTTPException(409, _taken(body.name))
    result = await check.test(tools, body.kind, body.connector_url, settings)
    if not result.ok or result.url is None:
        raise HTTPException(422, failed_detail(result))
    try:
        ds = await store.create(body.name, body.kind, result.url, body.description)
    except DuplicateName as exc:
        raise HTTPException(409, _taken(body.name)) from exc
    check.remember(ds, result)
    return _out(ds)


@router.get("")
async def list_datasources(store: Store) -> list[DataSourceOut]:
    return [_out(d) for d in await store.all()]


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def upload_files(
    store: Store,
    settings: Config,
    name: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> FileDataSourceOut:
    name = name.strip()
    if not name or len(name) > 100 or "\x00" in name:
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
        raise HTTPException(409, _taken(name)) from exc
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


@router.patch("/{datasource_id}")
async def update_datasource(
    datasource_id: str,
    body: UpdateDataSource,
    store: Store,
    tools: Tools,
    check: Check,
    settings: Config,
) -> DataSourceOut:
    """Name and description change as they are; a new address is tested first and
    nothing changes when the test fails. The address GET returns is masked, so sending
    it back counts as a new address."""
    ds = await _get(store, datasource_id)
    changes = body.model_dump(exclude_unset=True)
    name, url = changes.get("name"), changes.get("connector_url")
    if url is not None and same_url(url, ds.connector_url):
        url = None
    if name is not None and name != ds.name and await _name_taken(store, name):
        raise HTTPException(409, _taken(name))
    result = None
    if url is not None:
        if ds.kind == "file":
            raise HTTPException(422, FILE_URL)
        result = await check.test(tools, ds.kind, url, settings)
        if not result.ok or result.url is None:
            raise HTTPException(422, failed_detail(result))
        url = result.url
    try:
        updated = await store.update(
            datasource_id, name=name, description=changes.get("description"), connector_url=url
        )
    except DuplicateName as exc:
        raise HTTPException(409, _taken(name or ds.name)) from exc
    if updated is None:
        raise HTTPException(404, NOT_FOUND)
    if result is not None:
        check.forget(updated.datasource_id)
        check.remember(updated, result)
    return _out(updated)


@router.get("/{datasource_id}/health")
async def datasource_health(
    datasource_id: str,
    store: Store,
    tools: Tools,
    check: Check,
    settings: Config,
    fresh: bool = False,
) -> HealthOut:
    """Whether the connector answers, kept for a minute unless ``fresh``."""
    health = await check.health(tools, await _get(store, datasource_id), settings, fresh)
    return HealthOut(
        ok=health.ok,
        latency_ms=health.latency_ms,
        tool_count=health.tool_count,
        error=health.error,
        message=health.message,
        checked_at=health.checked_at.isoformat(),
    )


@router.get("/{datasource_id}/usage")
async def datasource_usage(datasource_id: str, store: Store, catalog: Catalog) -> UsageOut:
    """The flow apps whose current version names this source."""
    ds = await _get(store, datasource_id)
    found = await catalog.datasource_usage()
    return UsageOut(apps=[UsageApp(**use) for use in found.get(canon_id(ds.datasource_id), [])])


@router.get("/{datasource_id}/tools")
async def list_datasource_tools(
    datasource_id: str, store: Store, tools: Tools, check: Check, settings: Config
) -> list[ToolOut]:
    """The connector's tools; listing them also updates the source's health."""
    ds = await _get(store, datasource_id)
    try:
        listed = await check.tools(tools, ds, settings)
    except ConnectorUnavailable as exc:
        problem = connection_problem(exc.reason, settings.connector_check_seconds)
        raise HTTPException(502, problem.message) from exc
    except ToolError as exc:
        raise HTTPException(502, TOOLS_FAILED) from exc
    return [
        ToolOut(
            name=t.name,
            description=t.description,
            input_schema=t.input_schema,
            read_only=t.read_only,
        )
        for t in listed
    ]
