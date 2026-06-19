"""Health route. Always unauthenticated (see Section 3.4 rule 1)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, Response, status

from loom import constants
from loom.api.deps import get_pool
from loom.api.schemas import HealthResponse

if TYPE_CHECKING:
    from loom.db import DatabasePool

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(
    request: Request,
    response: Response,
    pool: DatabasePool = Depends(get_pool),
) -> HealthResponse:
    db = await pool.health_check_async()
    connected = bool(db.get("connected"))
    latency_ms = db.get("latency_ms")

    if not connected:
        health_status = "error"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif latency_ms is not None and latency_ms > constants.HEALTH_DEGRADED_LATENCY_MS:
        health_status = "degraded"
    else:
        health_status = "ok"

    uptime = time.monotonic() - request.app.state.started_at
    return HealthResponse(
        status=health_status,
        database=db,
        version=constants.APP_VERSION,
        uptime_seconds=round(uptime, 3),
    )
