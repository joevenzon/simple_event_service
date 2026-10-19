"""ASGI entry point for private event ingestion and session-protected history."""

import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.staticfiles import StaticFiles

from app.auth import hash_password
from app.clients import router as client_router
from app.config import Settings
from app.db import Database
from app.ingest import router as ingest_router
from app.login import router as login_router
from app.middleware import RequestGuards
from app.pages import router as page_router
from app.queries import router as query_router
from app.schemas import EventInput
from app.security import require_human
from app.worker import check_worker

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
        application.state.csrf_secret = (
            config.session_secret.get_secret_value().encode()
            if config.session_secret
            else secrets.token_bytes(32)
        )
        application.state.dummy_password_hash = hash_password(secrets.token_urlsafe(32))
        yield

    application = FastAPI(
        title="Simple Event Service",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(RequestGuards)
    application.include_router(login_router)
    application.include_router(ingest_router)
    application.include_router(query_router)
    application.include_router(client_router)
    application.include_router(page_router)
    application.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"
    )

    @application.get(
        "/api/v1/openapi.json", dependencies=[Depends(require_human)], include_in_schema=False
    )
    def openapi_schema():
        """Publish authenticated contracts for clients without exposing stored event data."""
        if application.openapi_schema is None:
            schema = get_openapi(
                title=application.title, version=application.version, routes=application.routes
            )
            event_schema = EventInput.model_json_schema(ref_template="#/components/schemas/{model}")
            # Ingestion parses two media types manually, so include its input model explicitly.
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            components.update(event_schema.pop("$defs", {}))
            components["EventInput"] = event_schema
            schema["components"]["securitySchemes"] = {
                "ClientBearer": {"type": "http", "scheme": "bearer"},
                "HumanSession": {"type": "apiKey", "in": "cookie", "name": "ses_session"},
            }

            # Document the same machine/human separation enforced by route dependencies.
            for path, operations in schema["paths"].items():
                for method, operation in operations.items():
                    if path.startswith("/api/v1/"):
                        scheme = "ClientBearer" if method == "post" else "HumanSession"
                        operation["security"] = [{scheme: []}]
            application.openapi_schema = schema
        return application.openapi_schema

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {
                "detail": [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
            status_code=422,
        )

    @application.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        logger.warning("Database request failed")
        return JSONResponse({"detail": "Storage unavailable"}, status_code=503)

    @application.exception_handler(OSError)
    async def storage_error(request: Request, exc: OSError):
        logger.warning("Filesystem request failed")
        return JSONResponse({"detail": "Storage unavailable"}, status_code=503)

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        try:
            request.app.state.database.check_ready()
            if request.app.state.settings.ntfy_topic is not None:
                check_worker(request.app.state.database)
        except (sqlite3.Error, OSError, RuntimeError):
            logger.warning("Database or worker readiness check failed")
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return application


app = create_app()
