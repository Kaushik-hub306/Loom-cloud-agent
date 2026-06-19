"""FastAPI dependency accessors.

These read already-constructed singletons off ``request.app.state`` (populated
during lifespan startup). They never read environment variables or build new
resources, keeping the request path cheap and the wiring centralized.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from loom.config import LoomConfig
    from loom.db import DatabasePool
    from loom.llm.router import LLMRouter
    from loom.memory.store import MemoryStore


def get_config(request: Request) -> LoomConfig:
    return request.app.state.config


def get_pool(request: Request) -> DatabasePool:
    return request.app.state.pool


def get_store(request: Request) -> MemoryStore:
    return request.app.state.store


def get_llm_router(request: Request) -> LLMRouter:
    return request.app.state.llm_router
