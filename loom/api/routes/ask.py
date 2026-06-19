"""Memory-augmented LLM ask route."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from loom import constants
from loom.api.auth import require_api_key
from loom.api.deps import get_config, get_llm_router, get_store
from loom.api.schemas import AskRequest, AskResponse
from loom.errors import LLMError
from loom.llm.prompts import ASK_SYSTEM_PROMPT
from loom.memory.formatting import format_session_context

if TYPE_CHECKING:
    from loom.config import LoomConfig
    from loom.llm.router import LLMRouter
    from loom.memory.store import MemoryStore

router = APIRouter(dependencies=[Depends(require_api_key)], tags=["ask"])


def _build_user_prompt(
    *, context_block: str, role: str, task: str, thread_history: str, message: str
) -> str:
    sections = [context_block, ""]
    if role.strip():
        sections.append(f"Your role: {role.strip()}")
    sections.append(f"Current task: {task.strip()}")
    if thread_history.strip():
        sections.append("")
        sections.append("Conversation so far:")
        sections.append(thread_history.strip())
    sections.append("")
    sections.append(f"User message: {message.strip()}")
    return "\n".join(sections)


@router.post("/ask", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    config: LoomConfig = Depends(get_config),
    store: MemoryStore = Depends(get_store),
    llm_router: LLMRouter = Depends(get_llm_router),
) -> AskResponse:
    # LLM-disabled is a 503 (recoverable/degraded), surfaced via the typed error
    # so the global handler renders the standard {error, message, request_id}.
    if config.llm_provider == "skip" or not config.llm_api_key:
        raise LLMError(
            "The /ask endpoint is disabled because no LLM provider is configured.",
            details={"reason": "llm_disabled"},
        )

    start = time.perf_counter()
    context = await store.get_session_context(
        task=payload.task,
        channel=payload.channel,
        workspace_id=payload.workspace_id,
        max_rules=payload.max_rules,
        max_contexts=payload.max_contexts,
    )
    context_block = format_session_context(context)
    user_prompt = _build_user_prompt(
        context_block=context_block,
        role=payload.role,
        task=payload.task,
        thread_history=payload.thread_history,
        message=payload.message,
    )

    result = await llm_router.complete(
        system=ASK_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=constants.DEFAULT_LLM_MAX_TOKENS,
        temperature=constants.DEFAULT_LLM_TEMPERATURE,
    )
    latency_ms = (time.perf_counter() - start) * 1000.0
    return AskResponse(
        response=result.text,
        model_used=result.model_used,
        memories_used=len(context.memories),
        contexts_used=len(context.contexts),
        usage=result.usage,
        latency_ms=round(latency_ms, 3),
    )
