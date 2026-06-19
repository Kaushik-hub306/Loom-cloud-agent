"""Public memory routes.

Per Phase 4 deliverable #5, the public read/write memory endpoints live here:
``/session_init``, ``/teach``, ``/recall``, ``/stats``, ``/export`` and
``/import``. All routes require ``X-Loom-Api-Key`` when configured (the
dependency is attached at router level). ``/health`` and ``/ask`` live in their
own modules; ``admin.py`` is intentionally minimal (see its docstring).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query

from loom.api.auth import require_api_key
from loom.api.deps import get_store
from loom.api.schemas import (
    ContextSchema,
    MemorySchema,
    RecallRequest,
    RecallResponse,
    SessionInitRequest,
    SessionInitResponse,
    StatsResponse,
    TeachRequest,
    TeachResponse,
)
from loom.memory.formatting import format_session_context

if TYPE_CHECKING:
    from loom.memory.store import MemoryStore

router = APIRouter(dependencies=[Depends(require_api_key)], tags=["memory"])


@router.post("/session_init", response_model=SessionInitResponse)
async def session_init(
    payload: SessionInitRequest,
    store: MemoryStore = Depends(get_store),
) -> SessionInitResponse:
    # Read-only: this endpoint must never write conversation context.
    start = time.perf_counter()
    context = await store.get_session_context(
        task=payload.task,
        channel=payload.channel,
        workspace_id=payload.workspace_id,
        max_rules=payload.max_rules,
        max_contexts=payload.max_contexts,
        include_contexts=payload.include_contexts,
    )
    latency_ms = (time.perf_counter() - start) * 1000.0
    return SessionInitResponse(
        memories=[MemorySchema.from_memory(m) for m in context.memories],
        contexts=[ContextSchema.from_context(c) for c in context.contexts],
        context_prompt=format_session_context(context),
        memory_count=len(context.memories),
        context_count=len(context.contexts),
        latency_ms=round(latency_ms, 3),
    )


@router.post("/teach", response_model=TeachResponse)
async def teach(
    payload: TeachRequest,
    store: MemoryStore = Depends(get_store),
) -> TeachResponse:
    result = await store.teach(
        domain=payload.domain,
        rule_type=payload.rule_type,
        rule=payload.rule,
        example=payload.example,
        confidence=payload.confidence,
        source_type=payload.source_type,
        sources=payload.sources,
        project=payload.project,
    )
    memory = result.memory
    if result.is_update:
        message = (
            f"Updated existing {memory.domain} {memory.rule_type} "
            "and increased confidence."
        )
    else:
        message = f"Remembered {memory.domain} {memory.rule_type}."
    return TeachResponse(
        id=memory.id,
        domain=memory.domain,
        confidence=memory.confidence,
        message=message,
        is_update=result.is_update,
    )


@router.post("/recall", response_model=RecallResponse)
async def recall(
    payload: RecallRequest,
    store: MemoryStore = Depends(get_store),
) -> RecallResponse:
    start = time.perf_counter()
    memories = await store.recall(
        query=payload.query,
        domain=payload.domain,
        min_confidence=payload.min_confidence,
        limit=payload.limit,
    )
    contexts = []
    if payload.include_contexts:
        contexts = await store.search_contexts(
            query=payload.query,
            channel=payload.channel or None,
            workspace_id=payload.workspace_id or None,
            limit=payload.limit,
        )
    latency_ms = (time.perf_counter() - start) * 1000.0
    return RecallResponse(
        memories=[MemorySchema.from_memory(m) for m in memories],
        contexts=[ContextSchema.from_context(c) for c in contexts],
        memory_count=len(memories),
        context_count=len(contexts),
        latency_ms=round(latency_ms, 3),
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(store: MemoryStore = Depends(get_store)) -> StatsResponse:
    s = await store.stats()
    return StatsResponse(
        total_rules=s.total_rules,
        rules_by_domain=s.rules_by_domain,
        rules_by_type=s.rules_by_type,
        total_uses=s.total_uses,
        active_contexts=s.active_contexts,
        active_blobs=s.active_blobs,
        expired_contexts=s.expired_contexts,
        expired_blobs=s.expired_blobs,
    )


@router.get("/export")
async def export_memories(
    include_embeddings: bool = Query(default=False),
    store: MemoryStore = Depends(get_store),
) -> dict:
    return await store.export_memories(include_embeddings=include_embeddings)


@router.post("/import")
async def import_memories(
    payload: dict,
    regenerate_embeddings: bool = Query(default=True),
    store: MemoryStore = Depends(get_store),
) -> dict:
    return await store.import_memories(
        payload, regenerate_embeddings=regenerate_embeddings
    )
