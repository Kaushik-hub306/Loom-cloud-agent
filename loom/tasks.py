"""Supervised background task helpers.

Every fire-and-forget ``asyncio.create_task`` in Loom must go through
``create_logged_task`` so exceptions are never silently swallowed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import structlog


def create_logged_task(
    coro: Awaitable[Any],
    *,
    logger: structlog.BoundLogger,
    name: str,
) -> asyncio.Task:
    """Create an asyncio task whose exceptions are logged on completion.

    Cancellation is logged at debug level; other exceptions at error level.
    """
    task = asyncio.ensure_future(coro)

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            logger.debug("background_task_cancelled", task_name=name)
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "background_task_failed",
                task_name=name,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    task.add_done_callback(_on_done)
    return task
