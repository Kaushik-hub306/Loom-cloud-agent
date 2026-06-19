"""Internal dataclasses for the memory layer.

API request/response shapes live in ``loom/api/schemas.py`` as Pydantic models.
These dataclasses are the in-process representation used by the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Memory:
    id: str
    domain: str
    rule_type: str
    rule: str
    example: str
    confidence: int
    uses: int
    sources: list[dict]
    source_type: str
    project: str
    created_at: datetime
    updated_at: datetime
    similarity: float | None = None


@dataclass(frozen=True)
class TeachResult:
    memory: Memory
    is_update: bool


@dataclass(frozen=True)
class ConversationContext:
    id: str
    workspace_id: str
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


@dataclass(frozen=True)
class SessionContext:
    memories: list[Memory]
    contexts: list[ConversationContext]


@dataclass(frozen=True)
class MemoryStats:
    total_rules: int
    rules_by_domain: dict[str, int]
    rules_by_type: dict[str, int]
    total_uses: int
    active_contexts: int
    active_blobs: int
    expired_contexts: int
    expired_blobs: int
    top_memories: list[Memory]
