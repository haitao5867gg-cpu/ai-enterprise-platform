from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aep import __version__
from aep.config import Settings, get_settings
from aep.health import check_postgres, check_redis


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AI Enterprise Platform", version=__version__)
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
        errors = {
            name: err
            for name, err in {
                "postgres": await check_postgres(settings),
                "redis": await check_redis(settings),
            }.items()
            if err is not None
        }
        checks = {name: ("error" if name in errors else "ok") for name in ("postgres", "redis")}
        status_code = 503 if errors else 200
        body = {"status": "error" if errors else "ok", "checks": checks, "errors": errors}
        return JSONResponse(body, status_code=status_code)

    return app


app = create_app()
