"""FastAPI application factory for the Loom memory service.

``create_app`` wires logging, the lifespan-managed resource singletons
(``DatabasePool``, ``EmbeddingService``, ``MemoryStore``, ``LLMRouter``), the
request-ID middleware, the global exception handlers, and all routers.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from loom import constants
from loom.api.routes import admin, ask, health, internal, memory
from loom.config import LoomConfig
from loom.db import DatabasePool
from loom.embeddings import EmbeddingService
from loom.errors import LoomError
from loom.llm.router import LLMRouter
from loom.logging_config import configure_logging
from loom.memory.store import MemoryStore

if TYPE_CHECKING:
    from starlette.responses import Response

logger = structlog.get_logger("loom.api.app")

# Map typed error codes to HTTP statuses. LLM-disabled (and other LLM failures)
# are surfaced as 503 per Section 9.6; auth failures as 401; bad input as 400;
# server-side faults as 500. Anything unmapped is treated as a 400 client error.
_STATUS_BY_CODE = {
    "api_auth_error": 401,
    "llm_error": 503,
    "import_export_error": 400,
    "database_error": 500,
    "embedding_error": 500,
    "config_error": 500,
}
_DEFAULT_LOOM_ERROR_STATUS = 400


def _sanitize_request_id(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()[: constants.REQUEST_ID_MAX_CHARS]
    return cleaned or None


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assign/propagate ``X-Request-ID`` and bind it to the log context.

    Also acts as the final safety net for genuinely unhandled exceptions:
    they are logged with a stack trace and converted to a stack-trace-free 500
    JSON body, guaranteeing every response carries an ``X-Request-ID`` header.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _sanitize_request_id(
            request.headers.get("X-Request-ID")
        ) or uuid.uuid4().hex
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - converted to safe 500 below
            logger.error(
                "unhandled_exception",
                error_type=type(exc).__name__,
                exc_info=exc,
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "message": "An internal error occurred.",
                    "request_id": request_id,
                },
            )
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers["X-Request-ID"] = request_id
        return response


async def _loom_error_handler(request: Request, exc: LoomError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    status_code = _STATUS_BY_CODE.get(exc.code, _DEFAULT_LOOM_ERROR_STATUS)
    logger.warning(
        "loom_error",
        code=exc.code,
        status=status_code,
        details=exc.details,
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "error": exc.code,
            "message": exc.user_message,
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id} if request_id else None,
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request validation failed.",
            "detail": exc.errors(),
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id} if request_id else None,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: LoomConfig = app.state.config

    pool = DatabasePool(config, mode="async")
    await pool.init_async()
    embeddings = EmbeddingService(config)
    store = MemoryStore(pool, embeddings, config)
    llm_router = LLMRouter(config)

    app.state.pool = pool
    app.state.embeddings = embeddings
    app.state.store = store
    app.state.llm_router = llm_router
    app.state.started_at = time.monotonic()

    logger.info("api_startup", **config.safe_summary())
    try:
        yield
    finally:
        await pool.close_async()
        http_client = getattr(app.state, "http_client", None)
        if http_client is not None:
            await http_client.aclose()
        logger.info("api_shutdown")


def create_app(config: LoomConfig | None = None) -> FastAPI:
    if config is None:
        config = LoomConfig.from_env()
    configure_logging(config)

    app = FastAPI(
        title="Loom Memory Service",
        version=constants.APP_VERSION,
        lifespan=lifespan,
    )
    app.state.config = config
    # Fallback so /health uptime works even before lifespan runs (overwritten there).
    app.state.started_at = time.monotonic()

    app.add_middleware(RequestIDMiddleware)

    app.add_exception_handler(LoomError, _loom_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    app.include_router(health.router)
    app.include_router(memory.router)
    app.include_router(ask.router)
    app.include_router(internal.router)
    app.include_router(admin.router)

    return app
