"""Projects API: a platform resource any app's runs may bind. Contract:
docs/api/projects.md."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from aep import policy
from aep.api.apps import Catalog
from aep.config import Settings, get_settings
from aep.projects import (
    DuplicateName,
    Project,
    ProjectBody,
    ProjectExists,
    ProjectStore,
)

# How a field's place reads in an error (docs/api/projects.md, "数据结构").
LABELS = {
    "project_id": "项目标识",
    "name": "名称",
    "description": "说明",
    "table_prefix": "表名前缀",
    "environments": "环境",
    "references": "引用表",
    "adopted": "归本项目的表",
    "params": "参数",
    "policy": "数据外发策略",
    "key": "标识",
    "label": "显示名",
    "catalog": "catalog",
    "schema": "schema",
    "cluster_id": "集群",
    "filter": "读取筛选",
    "type": "类型",
    "required": "必填",
    "default": "默认值",
    "choices": "选项",
    "value": "值",
    "samples": "样本级别",
    "models": "允许的模型",
}


def _where(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            parts.append(f"第 {part + 1} 项")
        else:
            parts.append(LABELS.get(str(part), str(part)))
    return "的".join(parts) or "请求"


def _sentence(error: dict[str, Any]) -> str:
    loc = tuple(error["loc"][1:])
    if error["type"] == "value_error" and (cause := error.get("ctx", {}).get("error")):
        return str(cause)
    if error["type"] == "extra_forbidden":
        return f"项目不接受「{_where(loc)}」这个设置"
    if error["type"] == "missing":
        return f"缺少「{_where(loc)}」"
    if error["type"] == "literal_error":
        return f"「{_where(loc)}」的取值不对"
    return f"「{_where(loc)}」的格式、长度或范围不正确"


class ProjectErrors(APIRoute):
    """Validation errors as one Chinese sentence that says where (``环境的第 1 项的标识``)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(422, _sentence(exc.errors()[0])) from exc

        return handle


router = APIRouter(prefix="/projects", tags=["projects"], route_class=ProjectErrors)


def get_store(request: Request) -> ProjectStore:
    return request.app.state.projects


Store = Annotated[ProjectStore, Depends(get_store)]
Config = Annotated[Settings, Depends(get_settings)]


def _not_found(project_id: str) -> HTTPException:
    return HTTPException(404, f"没有项目 {project_id}")


async def _check_models(body: ProjectBody, catalog: Any) -> None:
    """The policy's models must be on the gateway; not checked when the list is unknown."""
    if not body.policy.models:
        return
    known = await catalog.models()
    if known is None:
        return
    missing = [m for m in body.policy.models if m not in known and m not in policy.LOCAL_MODELS]
    if missing:
        raise HTTPException(422, f"模型网关上没有模型 {'、'.join(missing)}")


@router.get("")
async def list_projects(store: Store) -> list[dict[str, Any]]:
    return [p.summary() for p in await store.all()]


@router.get("/options")
async def project_options(catalog: Catalog, settings: Config) -> dict[str, Any]:
    default = policy.platform_default(settings)
    return {
        "sample_levels": [
            {
                "value": level,
                "label": policy.LABELS[level],
                "description": policy.DESCRIPTIONS[level],
            }
            for level in policy.LEVELS
        ],
        "models": await catalog.models(),
        "defaults": default.as_dict(),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectBody, store: Store, catalog: Catalog) -> dict[str, Any]:
    await _check_models(body, catalog)
    try:
        project = await store.create(body.body())
    except ProjectExists as exc:
        raise HTTPException(409, f"项目 {body.project_id} 已经存在") from exc
    except DuplicateName as exc:
        raise HTTPException(409, f"已经有叫「{body.name}」的项目") from exc
    return project.as_dict()


@router.get("/{project_id}")
async def get_project(project_id: str, store: Store) -> dict[str, Any]:
    project = await store.get(project_id)
    if project is None:
        raise _not_found(project_id)
    return project.as_dict()


@router.put("/{project_id}")
async def replace_project(
    project_id: str, body: ProjectBody, store: Store, catalog: Catalog
) -> dict[str, Any]:
    if body.project_id != project_id:
        raise HTTPException(422, "项目标识建好后不能改，要和地址里的一致")
    await _check_models(body, catalog)
    try:
        project: Project | None = await store.replace(body.body())
    except DuplicateName as exc:
        raise HTTPException(409, f"已经有叫「{body.name}」的项目") from exc
    if project is None:
        raise _not_found(project_id)
    return project.as_dict()


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, store: Store) -> Response:
    if not await store.delete(project_id):
        raise _not_found(project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
