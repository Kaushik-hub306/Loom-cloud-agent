"""Internal Slack-write routes.

Used only by the Slack worker over HTTP (Slack never touches the DB directly).
All routes require ``X-Loom-Internal-Token`` when configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response, status

from loom.api.auth import require_internal_token
from loom.api.deps import get_store
from loom.api.schemas import (
    ContextSummaryRequest,
    ContextSummaryResponse,
    ConversationBlobRequest,
    ConversationBlobResponse,
)

if TYPE_CHECKING:
    from loom.memory.store import MemoryStore

router = APIRouter(
    prefix="/internal",
    dependencies=[Depends(require_internal_token)],
    tags=["internal"],
)


@router.post("/conversation_blob", response_model=ConversationBlobResponse)
async def conversation_blob(
    payload: ConversationBlobRequest,
    response: Response,
    store: MemoryStore = Depends(get_store),
) -> ConversationBlobResponse:
    blob_id = await store.save_conversation_blob(
        channel=payload.channel,
        thread_ts=payload.thread_ts,
        messages=payload.messages,
        workspace_id=payload.workspace_id,
    )
    saved = bool(blob_id)
    if not saved:
        # Recoverable storage skip (the store logs the reason): 202 Accepted.
        response.status_code = status.HTTP_202_ACCEPTED
    return ConversationBlobResponse(id=blob_id, saved=saved)


@router.post("/context_summary", response_model=ContextSummaryResponse)
async def context_summary(
    payload: ContextSummaryRequest,
    response: Response,
    store: MemoryStore = Depends(get_store),
) -> ContextSummaryResponse:
    context_id = await store.save_context_summary(
        channel=payload.channel,
        thread_ts=payload.thread_ts,
        summary=payload.summary,
        domain=payload.domain,
        participants=payload.participants,
        message_count=payload.message_count,
        workspace_id=payload.workspace_id,
        is_new_topic=payload.is_new_topic,
    )
    saved = bool(context_id)
    if not saved:
        response.status_code = status.HTTP_202_ACCEPTED
    return ContextSummaryResponse(id=context_id, saved=saved)
