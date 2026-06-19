"""Pydantic v2 request/response schemas for the Loom FastAPI service.

These are the public HTTP contract. Internal store dataclasses live in
``loom/memory/models.py``; route handlers translate between the two.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from loom import constants
from loom.memory.models import ConversationContext, Memory


class MemorySchema(BaseModel):
    id: str
    domain: str
    rule_type: str
    rule: str
    example: str = ""
    confidence: int
    uses: int
    source_type: str
    project: str
    created_at: datetime
    updated_at: datetime
    similarity: float | None = None

    @classmethod
    def from_memory(cls, memory: Memory) -> MemorySchema:
        return cls(
            id=memory.id,
            domain=memory.domain,
            rule_type=memory.rule_type,
            rule=memory.rule,
            example=memory.example,
            confidence=memory.confidence,
            uses=memory.uses,
            source_type=memory.source_type,
            project=memory.project,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            similarity=memory.similarity,
        )


class ContextSchema(BaseModel):
    id: str
    workspace_id: str = ""
    channel: str
    thread_ts: str
    topic_index: int
    summary: str
    domain: str
    message_count: int
    participants: list[str]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    score: float | None = None

    @classmethod
    def from_context(cls, context: ConversationContext) -> ContextSchema:
        return cls(
            id=context.id,
            workspace_id=context.workspace_id,
            channel=context.channel,
            thread_ts=context.thread_ts,
            topic_index=context.topic_index,
            summary=context.summary,
            domain=context.domain,
            message_count=context.message_count,
            participants=list(context.participants),
            created_at=context.created_at,
            updated_at=context.updated_at,
            expires_at=context.expires_at,
            score=context.score,
        )


class SessionInitRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=constants.SESSION_TASK_MAX_CHARS)
    role: str = ""
    channel: str = ""
    workspace_id: str = ""
    max_rules: int = Field(default=10, ge=1, le=30)
    max_contexts: int = Field(default=3, ge=0, le=5)
    include_contexts: bool = True


class SessionInitResponse(BaseModel):
    memories: list[MemorySchema]
    contexts: list[ContextSchema]
    context_prompt: str
    memory_count: int
    context_count: int
    latency_ms: float


class TeachRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=constants.MEMORY_DOMAIN_MAX_CHARS)
    rule_type: str = Field(
        default="convention",
        min_length=1,
        max_length=constants.MEMORY_RULE_TYPE_MAX_CHARS,
    )
    rule: str = Field(
        ...,
        min_length=constants.MEMORY_RULE_MIN_CHARS,
        max_length=constants.MEMORY_RULE_MAX_CHARS,
    )
    example: str = Field(default="", max_length=constants.MEMORY_EXAMPLE_MAX_CHARS)
    confidence: int = Field(default=7, ge=1, le=10)
    source_type: str = "api_teach"
    sources: list[dict] = Field(default_factory=list)
    project: str = "default"


class TeachResponse(BaseModel):
    id: str
    domain: str
    confidence: int
    message: str
    is_update: bool


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=constants.SESSION_TASK_MAX_CHARS)
    domain: str | None = None
    channel: str = ""
    workspace_id: str = ""
    min_confidence: int = Field(default=3, ge=1, le=10)
    limit: int = Field(default=10, ge=1, le=30)
    include_contexts: bool = True


class RecallResponse(BaseModel):
    memories: list[MemorySchema]
    contexts: list[ContextSchema]
    memory_count: int
    context_count: int
    latency_ms: float


class AskRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=constants.SESSION_TASK_MAX_CHARS)
    message: str = Field(..., min_length=1, max_length=constants.ASK_MESSAGE_MAX_CHARS)
    role: str = ""
    thread_history: str = ""
    channel: str = ""
    workspace_id: str = ""
    max_rules: int = Field(default=10, ge=1, le=30)
    max_contexts: int = Field(default=3, ge=0, le=5)


class AskResponse(BaseModel):
    response: str
    model_used: str
    memories_used: int
    contexts_used: int
    usage: dict | None
    latency_ms: float


class ConversationBlobRequest(BaseModel):
    workspace_id: str = ""
    channel: str = Field(..., min_length=1)
    thread_ts: str = Field(..., min_length=1)
    messages: list[dict]


class ConversationBlobResponse(BaseModel):
    id: str
    saved: bool


class ContextSummaryRequest(BaseModel):
    workspace_id: str = ""
    channel: str = Field(..., min_length=1)
    thread_ts: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=10, max_length=1000)
    domain: str = Field(default="general", min_length=1, max_length=50)
    participants: list[str] = Field(default_factory=list)
    message_count: int = Field(default=0, ge=0)
    is_new_topic: bool = False


class ContextSummaryResponse(BaseModel):
    id: str
    saved: bool


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "error"]
    database: dict
    version: str
    uptime_seconds: float


class StatsResponse(BaseModel):
    total_rules: int
    rules_by_domain: dict[str, int]
    rules_by_type: dict[str, int]
    total_uses: int
    active_contexts: int
    active_blobs: int
    expired_contexts: int
    expired_blobs: int
