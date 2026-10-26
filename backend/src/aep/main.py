from contextlib import asynccontextmanager
from typing import Annotated

import openai
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from pydantic import BaseModel

from aep import __version__, llm
from aep.config import Settings, get_settings
from aep.health import check_llm_gateway, check_postgres, check_redis

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
        app.state.llm_client = llm.make_client(settings)
        yield
        await app.state.llm_client.close()

    app = FastAPI(title="AI Enterprise Platform", version=__version__, lifespan=lifespan)
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
