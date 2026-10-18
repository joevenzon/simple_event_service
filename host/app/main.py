"""ASGI entry point. Phase 0 exposes only liveness and database readiness."""

import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings
from app.db import Database

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            config = settings if settings is not None else Settings()
        except ValidationError:
            raise RuntimeError("Invalid SES settings; see docs/operations.md") from None
        logging.basicConfig(level=config.log_level.upper())
        database = Database(config.database_path)
        database.initialize()
        for directory in ("images", "thumbnails"):
            (config.data_dir / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        application.state.settings = config
        application.state.database = database
        yield

    application = FastAPI(
        title="Simple Event Service",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        try:
            request.app.state.database.check_ready()
        except (sqlite3.Error, OSError):
            logger.warning("Database readiness check failed")
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return application


app = create_app()
