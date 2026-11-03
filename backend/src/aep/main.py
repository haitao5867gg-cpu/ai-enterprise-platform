import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

import openai
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from openai import AsyncOpenAI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from aep import __version__, compute, llm, tracing
from aep.api import (
    app_access,
    apps,
    assets,
    auth,
    datasources,
    eval_sets,
    flows,
    knowledge,
    models,
    notifications,
    projects,
    runs,
    schedules,
    skills,
)
from aep.api import (
    catalog as catalog_api,
)
from aep.api import (
    ops as ops_api,
)
from aep.app_access import ApiKeys, PostgresApiKeyStore
from aep.app_mcp import AppMcpEndpoint
from aep.asset_catalog import AssetCatalog, PostgresAssetMetaStore
from aep.assets import AssetRepo, PostgresAssetStore
from aep.auth import AuthService, PostgresAuthStore, audit_middleware, authorize
from aep.config import Settings, get_settings
from aep.connector_check import ConnectorCheck
from aep.databricks import DevTarget
from aep.datasources import PostgresDataSourceStore, register_defaults
from aep.eval_sets import PostgresEvalSetStore
from aep.feedback import PostgresFeedbackStore
from aep.health import check_llm_gateway, check_postgres, check_redis, demo_checks
from aep.i18n import translated_http_error
from aep.knowledge.keywords import warm
from aep.knowledge.store import PostgresKnowledgeStore
from aep.knowledge.worker import KnowledgeWorker
from aep.models import GatewayAdmin, ModelService, PostgresCallStore
from aep.notify import Notifier, PostgresNotificationStore
from aep.ops import Ops, PostgresOpsStore, reuse_from
from aep.projects import PostgresProjectStore
from aep.runtime.catalog import AppCatalog, GatewayModels
from aep.runtime.flow.store import PostgresFlowAppStore, PostgresSecretStore
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import PostgresRunStore
from aep.runtime.versions import PostgresAppVersionStore
from aep.schedules import PostgresScheduleStore, Scheduler
from aep.skills import PostgresSkillStore
from aep.tools.mcp import McpTools

log = logging.getLogger(__name__)

SERVICES = ("postgres", "redis", "llm_gateway")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None


class ChatResponse(BaseModel):
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def get_llm_client(request: Request) -> AsyncOpenAI:
    return request.app.state.llm_client


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(warm)
        llm_client = llm.make_client(settings)
        # One pool for run records and LangGraph checkpoints; the saver needs
        # autocommit and dict rows.
        pool = AsyncConnectionPool(
            settings.database_url,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open(wait=True)
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        store = PostgresRunStore(pool)
        await store.setup()
        sources = PostgresDataSourceStore(pool)
        await sources.setup()
        await register_defaults(sources, settings.default_datasources)
        knowledge_store = PostgresKnowledgeStore(pool, settings.schedule_timezone)
        await knowledge_store.setup()
        asset_store = PostgresAssetStore(pool)
        await asset_store.setup()
        skill_store = PostgresSkillStore(pool)
        await skill_store.setup()
        await skill_store.register_builtins()
        version_store = PostgresAppVersionStore(pool)
        await version_store.setup()
        flow_store = PostgresFlowAppStore(pool)
        await flow_store.setup()
        secret_store = PostgresSecretStore(pool)
        await secret_store.setup()
        project_store = PostgresProjectStore(pool)
        await project_store.setup()
        tools = McpTools(settings.tool_timeout_seconds)
        asset_repo = AssetRepo.from_settings(settings, tools)
        tracer = tracing.Tracer.from_settings(settings)
        databricks = DevTarget.from_settings(settings)
        service = RunService(
            store,
            checkpointer,
            default_apps(),
            AppContext(
                llm_client,
                settings,
                datasources=sources,
                tools=tools,
                databricks=databricks,
                assets=asset_repo,
                asset_store=asset_store,
                knowledge=knowledge_store,
                projects=project_store,
            ),
            tracer,
            versions=version_store,
            skills=skill_store,
            flows=flow_store,
            secrets=secret_store,
        )
        # Flow apps' eval sets and people's feedback on runs (docs/api/evals.md).
        eval_set_store = PostgresEvalSetStore(pool)
        await eval_set_store.setup()
        feedback_store = PostgresFeedbackStore(pool)
        await feedback_store.setup()
        app.state.feedback = feedback_store
        catalog = AppCatalog(service, sources, settings, GatewayModels(llm_client), eval_set_store)
        # Models on the model page, the platform's default models and every call's record
        # (docs/api/models.md).
        call_store = PostgresCallStore(pool)
        service.calls = call_store
        gateway_admin = GatewayAdmin.from_settings(settings)

        def set_default_chat(alias: str) -> None:
            service.default_model = alias

        model_service = ModelService(
            gateway_admin,
            call_store,
            call_store,
            settings,
            llm_client,
            usage_of=catalog.model_usage,
            on_default_chat=set_default_chat,
        )
        await model_service.setup()
        # Sign-in, roles and the record of changes (docs/api/auth.md).
        auth_service = AuthService.from_settings(PostgresAuthStore(pool), settings)
        await auth_service.setup()
        app.state.auth = auth_service
        # Notices when a run waits for a person, finishes or fails
        # (docs/api/notifications.md).
        notifier = Notifier(PostgresNotificationStore(pool), settings, auth_service)
        await notifier.setup()
        service.on_settled = notifier.run_settled
        app.state.notifier = notifier
        # API keys for the app API and each app's MCP endpoint (docs/api/app-access.md).
        key_store = PostgresApiKeyStore(pool)
        await key_store.setup()
        app.state.api_keys = ApiKeys(key_store)
        llm.set_recorder(call_store.record)
        catalog.model_choices = model_service.choices
        if tracer.enabled:
            log.info("tracing runs to Langfuse at %s", settings.langfuse_host)
        if databricks is not None:
            log.info("validating ETL bundles on Databricks at %s", settings.databricks_host)
        if asset_repo is not None:
            log.info("opening ETL bundles as pull requests in %s", settings.assets_repo)
        recovered = await service.recover()
        if recovered:
            log.info("resumed %d runs left running by the previous process", recovered)
        if await catalog.recover():
            log.info("marked draft evals cut off by the previous process as interrupted")
        app.state.llm_client = llm_client
        app.state.run_service = service
        app.state.catalog = catalog
        # Every platform asset in one list (docs/api/catalog.md).
        asset_meta = PostgresAssetMetaStore(pool)
        await asset_meta.setup()
        asset_catalog = AssetCatalog(catalog, sources, asset_meta, asset_repo, settings)
        app.state.asset_catalog = asset_catalog
        # The operations dashboard, Databricks time and per-app model budgets
        # (docs/api/ops.md).
        ops_store = PostgresOpsStore(pool)
        await ops_store.setup()

        async def reuse() -> dict[str, Any]:
            return reuse_from(await asset_catalog.items())

        ops = Ops(
            store,
            call_store,
            ops_store,
            ops_store,
            call_store,
            service.app_titles,
            timezone=settings.schedule_timezone,
            reuse=reuse,
            notifier=notifier,
        )
        app.state.ops = ops
        compute.set_recorder(ops_store.record)
        llm.set_guard(ops.guard)
        app.state.models = model_service
        app.state.skills = skill_store
        app.state.datasources = sources
        app.state.projects = project_store
        app.state.asset_store = asset_store
        app.state.mcp_tools = tools
        app.state.databricks = databricks
        app.state.knowledge = knowledge_store
        knowledge_worker = KnowledgeWorker(knowledge_store, llm_client)
        app.state.knowledge_worker = knowledge_worker
        knowledge_task = asyncio.create_task(knowledge_worker.run())
        # Runs started on a schedule (docs/api/schedules.md).
        schedule_store = PostgresScheduleStore(pool)
        await schedule_store.setup()
        scheduler = Scheduler(
            schedule_store,
            schedules.starter(service),
            service.status_of,
            settings.schedule_poll_seconds,
        )
        app.state.schedules = schedule_store
        app.state.scheduler = scheduler
        schedule_task = asyncio.create_task(scheduler.run())
        try:
            yield
        finally:
            for task in (schedule_task, knowledge_task):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        try:
            await asyncio.wait_for(service.wait_idle(), timeout=10)
        except TimeoutError:
            log.warning("shutting down with runs still in progress; they resume on next start")
        tracer.shutdown()
        if databricks is not None:
            await databricks.close()
        if asset_repo is not None:
            await asset_repo.close()
        llm.set_recorder(None)
        llm.set_guard(None)
        compute.set_recorder(None)
        await gateway_admin.close()
        await pool.close()
        await llm_client.close()

    app = FastAPI(
        title="AI Enterprise Platform",
        version=__version__,
        lifespan=lifespan,
        # Who is asking and whether they may (docs/api/auth.md), before every route.
        dependencies=[Depends(authorize)],
    )
    app.middleware("http")(audit_middleware)
    # Error messages in the reader's language (Accept-Language), see aep.i18n.
    app.add_exception_handler(StarletteHTTPException, translated_http_error)
    # Connector tests and each data source's health (kept for a minute in this process).
    app.state.connector_check = ConnectorCheck()
    app.include_router(auth.router)
    app.include_router(runs.router)
    app.include_router(datasources.router)
    app.include_router(projects.router)
    app.include_router(assets.router)
    app.include_router(eval_sets.router)
    app.include_router(apps.router)
    app.include_router(flows.router)
    app.include_router(skills.router)
    app.include_router(knowledge.router)
    app.include_router(models.router)
    app.include_router(notifications.router)
    app.include_router(app_access.router)
    app.include_router(schedules.router)
    app.include_router(catalog_api.router)
    app.include_router(ops_api.router)
    # Each app as an MCP tool; it checks API keys itself.
    app.mount("/mcp/apps", AppMcpEndpoint())
    # Links back to a run (notices, the app API).
    app.state.web_url = settings.web_url
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(settings: Annotated[Settings, Depends(get_settings)]) -> JSONResponse:
        """Readiness: every backing service answers."""
        results = {
            "postgres": await check_postgres(settings),
            "redis": await check_redis(settings),
            "llm_gateway": await check_llm_gateway(settings),
        }
        errors = {name: err for name, err in results.items() if err is not None}
        checks = {name: ("error" if name in errors else "ok") for name in SERVICES}
        status_code = 503 if errors else 200
        body = {"status": "error" if errors else "ok", "checks": checks, "errors": errors}
        return JSONResponse(body, status_code=status_code)

    @app.get("/health/demo")
    async def health_demo(
        request: Request, settings: Annotated[Settings, Depends(get_settings)]
    ) -> list[dict[str, Any]]:
        """What the ETL demo needs (``make demo``): the model alias, the sample data
        source and the Databricks workspace; see docs/api/data-nodes.md."""
        state = request.app.state
        return await demo_checks(
            settings,
            await state.catalog.models(),
            state.datasources,
            state.mcp_tools,
            getattr(state, "databricks", None),
        )

    @app.post("/llm/chat")
    async def llm_chat(
        req: ChatRequest,
        request: Request,
        client: Annotated[AsyncOpenAI, Depends(get_llm_client)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> ChatResponse:
        """Send one chat completion through the gateway (for trying out model aliases)."""
        models = getattr(request.app.state, "models", None)
        model = req.model or (models.defaults()["chat"] if models else settings.default_model)
        try:
            result = await llm.chat(client, [m.model_dump() for m in req.messages], model=model)
        except openai.APIStatusError as exc:
            raise HTTPException(
                status_code=502, detail=f"model gateway error: {exc.message}"
            ) from exc
        except openai.APIConnectionError as exc:
            raise HTTPException(
                status_code=503, detail=f"model gateway unreachable: {exc}"
            ) from exc
        return ChatResponse(
            text=result.text,
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            total_tokens=result.usage.total_tokens,
        )

    return app


app = create_app()
