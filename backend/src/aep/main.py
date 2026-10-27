import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import openai
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from openai import AsyncOpenAI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from aep import __version__, llm, tracing
from aep.api import assets, datasources, runs
from aep.assets import AssetRepo, PostgresAssetStore
from aep.config import Settings, get_settings
from aep.databricks import DevTarget
from aep.datasources import PostgresDataSourceStore, register_defaults
from aep.health import check_llm_gateway, check_postgres, check_redis
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import PostgresRunStore
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
        asset_store = PostgresAssetStore(pool)
        await asset_store.setup()
        asset_repo = AssetRepo.from_settings(settings)
        tools = McpTools(settings.tool_timeout_seconds)
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
            ),
            tracer,
        )
        if tracer.enabled:
            log.info("tracing runs to Langfuse at %s", settings.langfuse_host)
        if databricks is not None:
            log.info("validating ETL bundles on Databricks at %s", settings.databricks_host)
        if asset_repo is not None:
            log.info("opening ETL bundles as pull requests in %s", settings.assets_repo)
        recovered = await service.recover()
        if recovered:
            log.info("resumed %d runs left running by the previous process", recovered)
        app.state.llm_client = llm_client
        app.state.run_service = service
        app.state.datasources = sources
        app.state.asset_store = asset_store
        app.state.mcp_tools = tools
        yield
        try:
            await asyncio.wait_for(service.wait_idle(), timeout=10)
        except TimeoutError:
            log.warning("shutting down with runs still in progress; they resume on next start")
        tracer.shutdown()
        if databricks is not None:
            await databricks.close()
        if asset_repo is not None:
            await asset_repo.close()
        await pool.close()
        await llm_client.close()

    app = FastAPI(title="AI Enterprise Platform", version=__version__, lifespan=lifespan)
    app.include_router(runs.router)
    app.include_router(datasources.router)
    app.include_router(assets.router)
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

    @app.post("/llm/chat")
    async def llm_chat(
        req: ChatRequest,
        client: Annotated[AsyncOpenAI, Depends(get_llm_client)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> ChatResponse:
        """Send one chat completion through the gateway (for trying out model aliases)."""
        model = req.model or settings.default_model
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
