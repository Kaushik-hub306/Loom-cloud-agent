"""MemoryStore: the only place (besides db.py) that touches the database.

All public methods that serve runtime reads/writes degrade gracefully: recall
and context operations never raise on runtime DB/embedding errors. ``teach``
validates input and may raise typed errors for invalid input.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from loom import constants
from loom.embeddings import vector_to_pg
from loom.errors import ImportExportError, LoomError
from loom.memory.models import (
    ConversationContext,
    Memory,
    MemoryStats,
    SessionContext,
    TeachResult,
)
from loom.tasks import create_logged_task
from loom.utils import make_memory_id, md5_short, slugify, utc_now

if TYPE_CHECKING:
    from loom.config import LoomConfig
    from loom.db import DatabasePool
    from loom.embeddings import EmbeddingService

logger = structlog.get_logger("loom.memory.store")

_COMMON_DOMAINS = ("coding", "architecture", "security", "testing")


def _coerce_json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


class MemoryStore:
    def __init__(
        self,
        pool: DatabasePool,
        embeddings: EmbeddingService,
        config: LoomConfig,
    ):
        self.pool = pool
        self.embeddings = embeddings
        self.config = config

    # ------------------------------------------------------------------
    # Row mapping
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_memory(row: Any) -> Memory:
        return Memory(
            id=row["id"],
            domain=row["domain"],
            rule_type=row["rule_type"],
            rule=row["rule"],
            example=row["example"],
            confidence=row["confidence"],
            uses=row["uses"],
            sources=_coerce_json_list(row["sources"]),
            source_type=row["source_type"],
            project=row["project"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            similarity=row["similarity"] if "similarity" in row.keys() else None,
        )

    @staticmethod
    def _row_to_context(row: Any) -> ConversationContext:
        participants = row["participants"]
        if participants is None:
            participants = []
        return ConversationContext(
            id=row["id"],
            workspace_id=row["workspace_id"],
            channel=row["channel"],
            thread_ts=row["thread_ts"],
            topic_index=row["topic_index"],
            summary=row["summary"],
            domain=row["domain"],
            message_count=row["message_count"],
            participants=list(participants),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            score=row["score"] if "score" in row.keys() else None,
        )

    # ------------------------------------------------------------------
    # Teach
    # ------------------------------------------------------------------
    async def teach(
        self,
        domain: str,
        rule_type: str,
        rule: str,
        example: str = "",
        confidence: int = 7,
        source_type: str = "user_teach",
        sources: list[dict] | None = None,
        project: str = "default",
    ) -> TeachResult:
        domain_s = (domain or "").strip()
        rule_type_s = (rule_type or "").strip() or "convention"
        rule_s = (rule or "").strip()
        example_s = (example or "").strip()
        sources = sources or []

        if not (1 <= len(domain_s) <= constants.MEMORY_DOMAIN_MAX_CHARS):
            raise LoomError(f"domain must be 1..{constants.MEMORY_DOMAIN_MAX_CHARS} chars.")
        if not (1 <= len(rule_type_s) <= constants.MEMORY_RULE_TYPE_MAX_CHARS):
            raise LoomError(
                f"rule_type must be 1..{constants.MEMORY_RULE_TYPE_MAX_CHARS} chars."
            )
        if not (constants.MEMORY_RULE_MIN_CHARS <= len(rule_s) <= constants.MEMORY_RULE_MAX_CHARS):
            raise LoomError(
                f"rule must be {constants.MEMORY_RULE_MIN_CHARS}.."
                f"{constants.MEMORY_RULE_MAX_CHARS} chars."
            )
        if len(example_s) > constants.MEMORY_EXAMPLE_MAX_CHARS:
            raise LoomError(
                f"example must be <= {constants.MEMORY_EXAMPLE_MAX_CHARS} chars."
            )
        if not (1 <= int(confidence) <= 10):
            raise LoomError("confidence must be an integer 1..10.")
        if not (source_type or "").strip():
            source_type = "user_teach"

        domain_norm = slugify(domain_s, max_chars=constants.MEMORY_DOMAIN_MAX_CHARS)
        rule_type_norm = slugify(rule_type_s, max_chars=constants.MEMORY_RULE_TYPE_MAX_CHARS)
        memory_id = make_memory_id(domain_norm, rule_type_norm, rule_s)

        embedding = await self.embeddings.embed(f"{rule_s} {example_s}".strip())
        embedding_pg = vector_to_pg(embedding)
        sources_json = json.dumps(sources)

        row = await self.pool.fetchrow(
            """
            INSERT INTO memories
                (id, domain, rule_type, rule, example, confidence, uses,
                 sources, source_type, embedding, project)
            VALUES ($1,$2,$3,$4,$5,$6,0,$7::jsonb,$8,$9::vector,$10)
            ON CONFLICT (id) DO UPDATE
            SET rule = EXCLUDED.rule,
                example = EXCLUDED.example,
                source_type = EXCLUDED.source_type,
                sources = EXCLUDED.sources,
                confidence = LEAST(
                    10,
                    GREATEST(memories.confidence, EXCLUDED.confidence) + 1
                ),
                embedding = COALESCE(EXCLUDED.embedding, memories.embedding)
            RETURNING memories.*, (xmax = '0'::xid) AS inserted
            """,
            memory_id, domain_norm, rule_type_norm, rule_s, example_s,
            int(confidence), sources_json, source_type, embedding_pg, project,
        )
        is_update = not bool(row["inserted"])
        logger.info("memory_taught", memory_id=memory_id, is_update=is_update)
        return TeachResult(memory=self._row_to_memory(row), is_update=is_update)

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------
    async def recall(
        self,
        query: str,
        domain: str | None = None,
        min_confidence: int = 3,
        limit: int = 10,
    ) -> list[Memory]:
        domain_norm = slugify(domain, max_chars=50) if domain else None
        try:
            memories = await self._recall_strategies(
                query, domain_norm, min_confidence, limit
            )
        except Exception as exc:  # noqa: BLE001 - recall must never raise
            logger.error("recall_failed", error_type=type(exc).__name__)
            return []

        if memories:
            self._schedule_use_increment([m.id for m in memories])
        return memories

    async def _recall_strategies(
        self, query: str, domain: str | None, min_confidence: int, limit: int
    ) -> list[Memory]:
        embedding = await self.embeddings.embed(query)

        if embedding is not None:
            rows = await self.pool.fetch(
                """
                SELECT *, 1 - (embedding <=> $1::vector) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL
                  AND confidence >= $2
                  AND ($3::text IS NULL OR domain = $3)
                ORDER BY embedding <=> $1::vector ASC, confidence DESC, uses DESC
                LIMIT $4
                """,
                vector_to_pg(embedding), min_confidence, domain, limit,
            )
            if rows:
                return [self._row_to_memory(r) for r in rows]

        # Full-text search.
        rows = await self.pool.fetch(
            """
            SELECT *, NULL::float8 AS similarity
            FROM memories
            WHERE confidence >= $2
              AND ($3::text IS NULL OR domain = $3)
              AND to_tsvector('english', rule || ' ' || example || ' ' ||
                  domain || ' ' || rule_type) @@ plainto_tsquery('english', $1)
            ORDER BY confidence DESC, uses DESC, updated_at DESC
            LIMIT $4
            """,
            query, min_confidence, domain, limit,
        )
        if rows:
            return [self._row_to_memory(r) for r in rows]

        # ILIKE fallback.
        rows = await self.pool.fetch(
            """
            SELECT *, NULL::float8 AS similarity
            FROM memories
            WHERE confidence >= $2
              AND ($3::text IS NULL OR domain = $3)
              AND (rule ILIKE '%' || $1 || '%' OR example ILIKE '%' || $1 || '%')
            ORDER BY confidence DESC, uses DESC, updated_at DESC
            LIMIT $4
            """,
            query, min_confidence, domain, limit,
        )
        if rows:
            return [self._row_to_memory(r) for r in rows]

        # Final fallback: highest-confidence rules in domain or common domains.
        rows = await self.pool.fetch(
            """
            SELECT *, NULL::float8 AS similarity
            FROM memories
            WHERE confidence >= $1
              AND ($2::text IS NULL OR domain = $2 OR domain = ANY($3::text[]))
            ORDER BY confidence DESC, uses DESC, updated_at DESC
            LIMIT $4
            """,
            min_confidence, domain, list(_COMMON_DOMAINS), limit,
        )
        return [self._row_to_memory(r) for r in rows]

    def _schedule_use_increment(self, ids: list[str]) -> None:
        if not ids:
            return
        try:
            create_logged_task(
                self._increment_uses(ids),
                logger=logger,
                name="increment_uses",
            )
        except RuntimeError:
            # No running loop (rare); skip increment silently-but-logged.
            logger.debug("use_increment_skipped_no_loop", count=len(ids))

    async def _increment_uses(self, ids: list[str]) -> None:
        try:
            await self.pool.execute(
                "UPDATE memories SET uses = uses + 1 WHERE id = ANY($1)", ids
            )
        except Exception as exc:  # noqa: BLE001 - logged, never fails recall
            logger.warning("use_increment_failed", error_type=type(exc).__name__)

    # ------------------------------------------------------------------
    # Session context
    # ------------------------------------------------------------------
    async def get_session_context(
        self,
        task: str,
        channel: str = "",
        workspace_id: str = "",
        max_rules: int | None = None,
        max_contexts: int | None = None,
        include_contexts: bool = True,
    ) -> SessionContext:
        max_rules = max_rules if max_rules is not None else self.config.max_rules_per_session
        max_contexts = (
            max_contexts if max_contexts is not None
            else self.config.max_contexts_per_session
        )

        if not include_contexts or max_contexts <= 0:
            memories = await self._safe_recall(task, max_rules)
            return SessionContext(memories=memories, contexts=[])

        memories_task = self._safe_recall(task, max_rules)
        contexts_task = self._safe_search_contexts(
            task, channel or None, workspace_id or None, max_contexts
        )
        memories, contexts = await asyncio.gather(memories_task, contexts_task)
        return SessionContext(memories=memories, contexts=contexts)

    async def _safe_recall(self, task: str, limit: int) -> list[Memory]:
        try:
            return await self.recall(task, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.error("session_recall_failed", error_type=type(exc).__name__)
            return []

    async def _safe_search_contexts(
        self, task: str, channel: str | None, workspace_id: str | None, limit: int
    ) -> list[ConversationContext]:
        try:
            return await self.search_contexts(task, channel, workspace_id, limit)
        except Exception as exc:  # noqa: BLE001
            logger.error("session_contexts_failed", error_type=type(exc).__name__)
            return []

    # ------------------------------------------------------------------
    # Conversation blob
    # ------------------------------------------------------------------
    async def save_conversation_blob(
        self,
        channel: str,
        thread_ts: str,
        messages: list[dict],
        workspace_id: str = "",
    ) -> str:
        try:
            blob_id = md5_short(f"{workspace_id}:{channel}:{thread_ts}")
            sanitized = self._sanitize_messages(messages)[-constants.BLOB_MAX_MESSAGES:]
            messages_json = json.dumps(sanitized)
            await self.pool.execute(
                """
                INSERT INTO conversation_blobs
                    (id, workspace_id, channel, thread_ts, messages, message_count,
                     expires_at)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,
                        NOW() + ($7 || ' days')::interval)
                ON CONFLICT (workspace_id, channel, thread_ts) DO UPDATE
                SET messages = EXCLUDED.messages,
                    message_count = EXCLUDED.message_count,
                    expires_at = EXCLUDED.expires_at
                """,
                blob_id, workspace_id, channel, thread_ts, messages_json,
                len(sanitized), str(self.config.blob_ttl_days),
            )
            return blob_id
        except Exception as exc:  # noqa: BLE001 - never raise on runtime failure
            logger.error("save_blob_failed", error_type=type(exc).__name__)
            return ""

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        allowed = ("user", "text", "ts", "thread_ts", "is_bot")
        cleaned: list[dict] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            cleaned.append({k: msg.get(k) for k in allowed})
        return cleaned

    # ------------------------------------------------------------------
    # Context summary
    # ------------------------------------------------------------------
    async def save_context_summary(
        self,
        channel: str,
        thread_ts: str,
        summary: str,
        domain: str = "general",
        participants: list[str] | None = None,
        message_count: int = 0,
        workspace_id: str = "",
        is_new_topic: bool = False,
    ) -> str:
        try:
            summary_s = (summary or "").strip()[: self.config.context_max_chars]
            if len(summary_s) < constants.CONTEXT_SUMMARY_MIN_CHARS:
                logger.info("context_summary_too_short", length=len(summary_s))
                return ""
            domain_norm = slugify(domain or "general", max_chars=50) or "general"
            participants = participants or []

            embedding = await self.embeddings.embed(summary_s)
            embedding_pg = vector_to_pg(embedding)

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    if is_new_topic:
                        lock_key = f"{workspace_id}:{channel}:{thread_ts}"
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtext($1))",
                            lock_key,
                        )
                        max_row = await conn.fetchrow(
                            """
                            SELECT COALESCE(MAX(topic_index), -1) AS max_idx
                            FROM conversation_contexts
                            WHERE workspace_id = $1 AND channel = $2 AND thread_ts = $3
                            """,
                            workspace_id, channel, thread_ts,
                        )
                        topic_index = int(max_row["max_idx"]) + 1
                    else:
                        topic_index = 0

                    context_id = md5_short(
                        f"{workspace_id}:{channel}:{thread_ts}:{topic_index}"
                    )
                    await conn.execute(
                        """
                        INSERT INTO conversation_contexts
                            (id, workspace_id, channel, thread_ts, topic_index, summary,
                             embedding, domain, message_count, participants, expires_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7::vector,$8,$9,$10,
                                NOW() + ($11 || ' days')::interval)
                        ON CONFLICT (workspace_id, channel, thread_ts, topic_index) DO UPDATE
                        SET summary = EXCLUDED.summary,
                            embedding = EXCLUDED.embedding,
                            domain = EXCLUDED.domain,
                            message_count = EXCLUDED.message_count,
                            participants = EXCLUDED.participants,
                            expires_at = EXCLUDED.expires_at
                        """,
                        context_id, workspace_id, channel, thread_ts, topic_index,
                        summary_s, embedding_pg, domain_norm, message_count,
                        participants, str(self.config.context_ttl_days),
                    )
            return context_id
        except Exception as exc:  # noqa: BLE001 - never raise on runtime failure
            logger.error("save_context_failed", error_type=type(exc).__name__)
            return ""

    # ------------------------------------------------------------------
    # Context search
    # ------------------------------------------------------------------
    async def search_contexts(
        self,
        query: str,
        channel: str | None = None,
        workspace_id: str | None = None,
        limit: int = 5,
    ) -> list[ConversationContext]:
        try:
            embedding = await self.embeddings.embed(query)
            if embedding is not None:
                rows = await self.pool.fetch(
                    """
                    SELECT *,
                           (1 - (embedding <=> $1::vector))
                           * exp(-1 * (EXTRACT(EPOCH FROM (NOW() - updated_at))
                             / 86400.0) * ln(2) / $2) AS score
                    FROM conversation_contexts
                    WHERE expires_at > NOW()
                      AND embedding IS NOT NULL
                      AND ($3::text IS NULL OR channel = $3)
                      AND ($4::text IS NULL OR workspace_id = $4)
                    ORDER BY score DESC
                    LIMIT $5
                    """,
                    vector_to_pg(embedding), self.config.context_half_life_days,
                    channel, workspace_id, limit,
                )
                if rows:
                    return [self._row_to_context(r) for r in rows]

            rows = await self.pool.fetch(
                """
                SELECT *, NULL::float8 AS score
                FROM conversation_contexts
                WHERE expires_at > NOW()
                  AND ($2::text IS NULL OR channel = $2)
                  AND ($3::text IS NULL OR workspace_id = $3)
                  AND to_tsvector('english', summary || ' ' || domain)
                      @@ plainto_tsquery('english', $1)
                ORDER BY updated_at DESC
                LIMIT $4
                """,
                query, channel, workspace_id, limit,
            )
            if rows:
                return [self._row_to_context(r) for r in rows]

            rows = await self.pool.fetch(
                """
                SELECT *, NULL::float8 AS score
                FROM conversation_contexts
                WHERE expires_at > NOW()
                  AND ($2::text IS NULL OR channel = $2)
                  AND ($3::text IS NULL OR workspace_id = $3)
                  AND summary ILIKE '%' || $1 || '%'
                ORDER BY updated_at DESC
                LIMIT $4
                """,
                query, channel, workspace_id, limit,
            )
            if rows:
                return [self._row_to_context(r) for r in rows]

            rows = await self.pool.fetch(
                """
                SELECT *, NULL::float8 AS score
                FROM conversation_contexts
                WHERE expires_at > NOW()
                  AND ($1::text IS NULL OR channel = $1)
                  AND ($2::text IS NULL OR workspace_id = $2)
                ORDER BY updated_at DESC
                LIMIT $3
                """,
                channel, workspace_id, limit,
            )
            return [self._row_to_context(r) for r in rows]
        except Exception as exc:  # noqa: BLE001 - never raise
            logger.error("search_contexts_failed", error_type=type(exc).__name__)
            return []

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    async def stats(self) -> MemoryStats:
        total_rules = await self.pool.fetchval("SELECT COUNT(*) FROM memories")
        total_uses = await self.pool.fetchval(
            "SELECT COALESCE(SUM(uses), 0) FROM memories"
        )
        domain_rows = await self.pool.fetch(
            "SELECT domain, COUNT(*) AS c FROM memories GROUP BY domain"
        )
        type_rows = await self.pool.fetch(
            "SELECT rule_type, COUNT(*) AS c FROM memories GROUP BY rule_type"
        )
        active_contexts = await self.pool.fetchval(
            "SELECT COUNT(*) FROM conversation_contexts WHERE expires_at > NOW()"
        )
        expired_contexts = await self.pool.fetchval(
            "SELECT COUNT(*) FROM conversation_contexts WHERE expires_at <= NOW()"
        )
        active_blobs = await self.pool.fetchval(
            "SELECT COUNT(*) FROM conversation_blobs WHERE expires_at > NOW()"
        )
        expired_blobs = await self.pool.fetchval(
            "SELECT COUNT(*) FROM conversation_blobs WHERE expires_at <= NOW()"
        )
        top_rows = await self.pool.fetch(
            """
            SELECT *, NULL::float8 AS similarity
            FROM memories
            ORDER BY uses DESC, confidence DESC, updated_at DESC
            LIMIT 5
            """
        )
        return MemoryStats(
            total_rules=int(total_rules or 0),
            rules_by_domain={r["domain"]: int(r["c"]) for r in domain_rows},
            rules_by_type={r["rule_type"]: int(r["c"]) for r in type_rows},
            total_uses=int(total_uses or 0),
            active_contexts=int(active_contexts or 0),
            active_blobs=int(active_blobs or 0),
            expired_contexts=int(expired_contexts or 0),
            expired_blobs=int(expired_blobs or 0),
            top_memories=[self._row_to_memory(r) for r in top_rows],
        )

    # ------------------------------------------------------------------
    # Export / import
    # ------------------------------------------------------------------
    async def export_memories(self, *, include_embeddings: bool = False) -> dict:
        rows = await self.pool.fetch("SELECT * FROM memories ORDER BY id")
        memories = []
        for row in rows:
            item = {
                "id": row["id"],
                "domain": row["domain"],
                "rule_type": row["rule_type"],
                "rule": row["rule"],
                "example": row["example"],
                "confidence": row["confidence"],
                "uses": row["uses"],
                "sources": _coerce_json_list(row["sources"]),
                "source_type": row["source_type"],
                "project": row["project"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            if include_embeddings:
                from loom.embeddings import pg_to_vector

                item["embedding"] = pg_to_vector(row["embedding"])
            memories.append(item)
        return {
            "version": constants.EXPORT_SCHEMA_VERSION,
            "exported_at": _iso(utc_now()),
            "include_embeddings": include_embeddings,
            "memories": memories,
        }

    async def import_memories(
        self, payload: dict, *, regenerate_embeddings: bool = True
    ) -> dict:
        version = payload.get("version")
        if version != constants.EXPORT_SCHEMA_VERSION:
            raise ImportExportError(
                f"Unsupported export version: {version!r}. "
                f"Expected {constants.EXPORT_SCHEMA_VERSION}."
            )
        memories = payload.get("memories")
        if not isinstance(memories, list):
            raise ImportExportError("Export payload missing 'memories' list.")

        imported = updated = skipped = failed = 0
        for item in memories:
            try:
                result = await self._import_one(item, regenerate_embeddings)
                if result == "imported":
                    imported += 1
                elif result == "updated":
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 - per-item resilience
                failed += 1
                logger.warning("import_item_failed", error_type=type(exc).__name__)
        return {
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
        }

    async def _import_one(self, item: dict, regenerate_embeddings: bool) -> str:
        domain = (item.get("domain") or "").strip()
        rule = (item.get("rule") or "").strip()
        if not domain or not rule:
            return "skipped"
        rule_type = (item.get("rule_type") or "convention").strip()
        example = (item.get("example") or "").strip()
        confidence = int(item.get("confidence", 7))
        confidence = min(10, max(1, confidence))
        source_type = (item.get("source_type") or "import").strip()
        sources = item.get("sources") or []
        project = (item.get("project") or "default").strip()

        domain_norm = slugify(domain, max_chars=50)
        rule_type_norm = slugify(rule_type, max_chars=50)
        memory_id = make_memory_id(domain_norm, rule_type_norm, rule)

        embedding_pg = None
        provided_embedding = item.get("embedding")
        if not regenerate_embeddings and isinstance(provided_embedding, list):
            if len(provided_embedding) == self.config.embedding_dimension:
                embedding_pg = vector_to_pg([float(v) for v in provided_embedding])
        else:
            embedding = await self.embeddings.embed(f"{rule} {example}".strip())
            embedding_pg = vector_to_pg(embedding)

        existing = await self.pool.fetchrow(
            "SELECT id FROM memories WHERE id = $1", memory_id
        )
        sources_json = json.dumps(sources)
        await self.pool.execute(
            """
            INSERT INTO memories
                (id, domain, rule_type, rule, example, confidence, uses,
                 sources, source_type, embedding, project)
            VALUES ($1,$2,$3,$4,$5,$6,0,$7::jsonb,$8,$9::vector,$10)
            ON CONFLICT (id) DO UPDATE
            SET rule = EXCLUDED.rule,
                example = EXCLUDED.example,
                confidence = GREATEST(memories.confidence, EXCLUDED.confidence),
                sources = EXCLUDED.sources,
                source_type = EXCLUDED.source_type,
                embedding = COALESCE(EXCLUDED.embedding, memories.embedding)
            """,
            memory_id, domain_norm, rule_type_norm, rule, example, confidence,
            sources_json, source_type, embedding_pg, project,
        )
        return "updated" if existing else "imported"


def _iso(value: datetime) -> str:
    if value is None:
        return ""
    return value.astimezone().isoformat() if value.tzinfo else value.isoformat()
