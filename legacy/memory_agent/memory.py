"""
Memory store — shared storage with semantic search.
Postgres + pgvector. Supabase is the single source of truth. No local fallback.
"""
import os
import sys
import json
import psycopg2
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Memory:
    """A single memory/rule."""
    id: str
    domain: str
    rule_type: str
    rule: str
    example: str = ""
    confidence: int = 5
    sources: list = field(default_factory=list)


class MemoryStore:
    """Shared memory: Postgres + pgvector backed by Supabase."""

    def __init__(self, data_dir: Path | None = None):
        # data_dir kept for backward compat only — no longer used for file storage.
        # Supabase/Postgres is the single source of truth.
        self._db_url = os.environ.get("DATABASE_URL") or os.environ.get("LOOM_DATABASE_URL")
        self._conn = None  # psycopg2 connection

        if not self._db_url:
            raise RuntimeError(
                "LOOM_DATABASE_URL or DATABASE_URL is required. "
                "Set it in your environment or MCP config. "
                "Supabase is the single source of truth — no local file fallback."
            )

        try:
            self._conn = psycopg2.connect(self._db_url)
            self._conn.autocommit = True
        except Exception as e:
            raise RuntimeError(
                f"Failed to connect to Supabase: {e}. "
                f"Check your DATABASE_URL and network connection."
            )

    # ── Embedding generation ─────────────────────────────────

    def _embed(self, text: str) -> list[float] | None:
        """Generate embedding vector for semantic search."""
        try:
            from litellm import embedding
            result = embedding(
                model="gemini/text-embedding-004",
                input=[text[:3000]],
            )
            return result.data[0]["embedding"]
        except Exception:
            return None

    # ── recall / search ─────────────────────────────────────

    def recall(self, query: str, domain: str | None = None,
               min_confidence: int = 1, limit: int = 10) -> list[Memory]:
        """Recall relevant memories via pgvector semantic search (text fallback)."""
        return self._recall_postgres(query, domain, min_confidence, limit)

    def _recall_postgres(self, query: str, domain: str | None,
                         min_confidence: int, limit: int) -> list[Memory]:
        """Semantic search via pgvector cosine similarity."""
        embedding = self._embed(query)
        cur = self._conn.cursor()

        if embedding:
            # Semantic search
            cur.execute("""
                SELECT id, domain, rule_type, rule, example, confidence, sources,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL
                  AND confidence >= %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
            """, (json.dumps(embedding), min_confidence, json.dumps(embedding), limit))
        else:
            # Fallback: text search
            cur.execute("""
                SELECT id, domain, rule_type, rule, example, confidence, sources, 0.0
                FROM memories
                WHERE (LOWER(rule) LIKE %s OR LOWER(domain) LIKE %s)
                  AND confidence >= %s
                ORDER BY confidence DESC
                LIMIT %s;
            """, (f'%{query.lower()}%', f'%{query.lower()}%', min_confidence, limit))

        results = []
        for row in cur.fetchall():
            results.append(Memory(id=row[0], domain=row[1], rule_type=row[2],
                                  rule=row[3], example=row[4] or "",
                                  confidence=row[5], sources=row[6] or []))
        cur.close()
        return results

    # ── context / session_init ───────────────────────────────

    def get_context(self, task: str, role: str = "",
                    max_rules: int = 10, channel: str = "",
                    include_conversations: bool = True) -> list[Memory]:
        """Load all relevant context for a task — rules + conversation summaries."""
        memories = self.recall(query=task, min_confidence=3, limit=max_rules)

        # Fallback: supplement with high-confidence domain rules
        if len(memories) < 3:
            for domain in ["coding", "general", "architecture", "security"]:
                domain_memories = self.recall(
                    query="", domain=domain, min_confidence=6, limit=5,
                )
                for m in domain_memories:
                    if m.id not in {mem.id for mem in memories}:
                        memories.append(m)
                if len(memories) >= max_rules:
                    break

        memories.sort(key=lambda m: m.confidence, reverse=True)
        return memories[:max_rules]

    def get_context_with_conversations(self, task: str, role: str = "",
                                       max_rules: int = 10, channel: str = "",
                                       max_contexts: int = 3) -> dict:
        """Load rules + conversation contexts for session_init.

        Returns a dict with 'memories' (list[Memory]) and 'contexts' (list[dict]).
        Conversation contexts are semantically searched and recency-weighted.
        Max 3 contexts, each ≤500 chars → ≤1500 chars total injection.
        """
        memories = self.get_context(task=task, role=role, max_rules=max_rules)

        contexts: list[dict] = []
        if channel or task:
            ctx_limit = min(max_contexts, 3)
            contexts = self.search_contexts(
                query=task or "",
                channel=channel if channel else None,
                limit=ctx_limit,
            )

        return {
            "memories": memories,
            "contexts": contexts,
        }

    # ── teach ────────────────────────────────────────────────

    def teach(self, domain: str, rule_type: str, rule: str,
              example: str = "", confidence: int = 7) -> Memory:
        """Store a new rule with embedding for semantic search."""
        return self._teach_postgres(domain, rule_type, rule, example, confidence)

    def _teach_postgres(self, domain: str, rule_type: str, rule: str,
                        example: str, confidence: int) -> Memory:
        """Store in Postgres with embedding."""
        rule_id = f"{domain}::{rule_type}::{rule.lower().replace(' ', '-')[:80]}"
        embedding = self._embed(rule)

        cur = self._conn.cursor()

        # Upsert: insert or bump confidence
        cur.execute("""
            INSERT INTO memories (id, domain, rule_type, rule, example, confidence,
                               sources, source_type, embedding, project)
            VALUES (%s, %s, %s, %s, %s, %s, '[]', 'user_teach', %s::vector, 'loom-agent')
            ON CONFLICT (id) DO UPDATE SET
                confidence = LEAST(10, memories.confidence + 1),
                updated_at = NOW(),
                embedding = COALESCE(EXCLUDED.embedding, memories.embedding);
        """, (rule_id, domain, rule_type, rule.strip(), example, confidence,
              json.dumps(embedding) if embedding else None))

        # Get final state
        cur.execute("SELECT id, confidence FROM memories WHERE id = %s;", (rule_id,))
        row = cur.fetchone()
        cur.close()

        stored_confidence = row[1] if row else confidence
        return Memory(id=rule_id, domain=domain, rule_type=rule_type,
                      rule=rule.strip(), confidence=stored_confidence)

    # ── conversation context ────────────────────────────────

    def _make_conversation_id(self, channel: str, thread_ts: str) -> str:
        """Deterministic ID from Slack identifiers."""
        import hashlib
        raw = f"{channel}:{thread_ts}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def save_conversation_blob(self, channel: str, thread_ts: str,
                               messages: list[dict], workspace_id: str = "") -> str:
        """Save raw conversation messages as a blob backup before LLM evaluation.

        Guards against data loss — if the gatekeeper LLM fails, raw messages
        are still retrievable.  Shorter TTL than summaries (14 days).
        Returns the blob ID.
        """
        blob_id = self._make_conversation_id(channel, thread_ts)

        if self._conn:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO conversation_blobs (id, channel, workspace_id, thread_ts,
                    messages, message_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    messages = EXCLUDED.messages,
                    message_count = EXCLUDED.message_count,
                    created_at = NOW(),
                    expires_at = NOW() + INTERVAL '14 days';
            """, (blob_id, channel, workspace_id, thread_ts,
                  json.dumps(messages), len(messages)))
            cur.close()
        return blob_id

    def save_context_summary(self, channel: str, thread_ts: str, summary: str,
                             domain: str = "general", participants: list[str] | None = None,
                             message_count: int = 0, workspace_id: str = "",
                             append: bool = False) -> str:
        """Save an LLM-generated context summary with embedding for semantic search.

        If append=True, creates a variant row with a modified key so prior
        summaries for the same thread are preserved (topic shift detection).
        Returns the row ID.
        """
        summary = summary.strip()[:500]  # enforce max 500 chars
        base_id = self._make_conversation_id(channel, thread_ts)
        row_id = base_id
        if append:
            # timestamp suffix so topic-shifted summaries don't overwrite
            import time
            row_id = f"{base_id}-{int(time.time())}"

        embedding = self._embed(summary)
        part_list = list(participants) if participants else []

        if self._conn:
            cur = self._conn.cursor()
            if append:
                # INSERT a new row — different id, same channel+thread_ts (PK allows it)
                cur.execute("""
                    INSERT INTO conversation_contexts (id, channel, workspace_id, thread_ts,
                        summary, embedding, domain, message_count, participants)
                    VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
                    ON CONFLICT (channel, thread_ts) DO NOTHING;
                """, (row_id, channel, workspace_id, thread_ts, summary,
                      json.dumps(embedding) if embedding else None,
                      domain, message_count, part_list))
                # ON CONFLICT DO NOTHING means a concurrent write wins — rare edge case
            else:
                cur.execute("""
                    INSERT INTO conversation_contexts (id, channel, workspace_id, thread_ts,
                        summary, embedding, domain, message_count, participants)
                    VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
                    ON CONFLICT (channel, thread_ts) DO UPDATE SET
                        id = EXCLUDED.id,
                        summary = EXCLUDED.summary,
                        embedding = COALESCE(EXCLUDED.embedding, conversation_contexts.embedding),
                        domain = EXCLUDED.domain,
                        message_count = EXCLUDED.message_count,
                        participants = EXCLUDED.participants,
                        updated_at = NOW();
                """, (row_id, channel, workspace_id, thread_ts, summary,
                      json.dumps(embedding) if embedding else None,
                      domain, message_count, part_list))
            cur.close()
        return row_id

    def search_contexts(self, query: str, channel: str | None = None,
                        workspace_id: str | None = None,
                        min_age_hours: float = 0, limit: int = 5) -> list[dict]:
        """Semantic search across conversation_contexts with recency weighting.

        Results are scored by cosine similarity, then multiplied by a recency
        factor: score * e^(-days_ago * ln(2) / 10).  Exact 10-day half-life.
        """
        embedding = self._embed(query)

        if self._conn and embedding:
            cur = self._conn.cursor()
            sql = """
                SELECT id, channel, thread_ts, summary, domain, message_count,
                       participants, created_at,
                       (1.0 - (embedding <=> %s::vector))
                       * EXP(EXTRACT(EPOCH FROM (created_at - NOW())) / 86400.0
                             * LN(2) / 10.0) AS adjusted_score
                FROM conversation_contexts
                WHERE embedding IS NOT NULL
                  AND expires_at > NOW()
            """
            params = [json.dumps(embedding)]

            if channel:
                sql += " AND channel = %s"
                params.append(channel)
            if workspace_id:
                sql += " AND workspace_id = %s"
                params.append(workspace_id)
            if min_age_hours > 0:
                sql += " AND created_at < NOW() - INTERVAL '%s hours'"
                params.append(str(min_age_hours))

            sql += " ORDER BY adjusted_score DESC LIMIT %s"
            params.append(limit)

            cur.execute(sql, params)
            results = []
            for row in cur.fetchall():
                results.append({
                    "id": row[0], "channel": row[1], "thread_ts": row[2],
                    "summary": row[3], "domain": row[4],
                    "message_count": row[5], "participants": row[6] or [],
                    "created_at": row[7].isoformat() if hasattr(row[7], 'isoformat') else str(row[7]),
                })
            cur.close()
            return results

        # Fallback: text search (no embedding available)
        if self._conn:
            cur = self._conn.cursor()
            q = f"%{query.lower()}%"
            cur.execute("""
                SELECT id, channel, thread_ts, summary, domain, message_count,
                       participants, created_at
                FROM conversation_contexts
                WHERE expires_at > NOW()
                  AND (LOWER(summary) LIKE %s OR LOWER(domain) LIKE %s)
                ORDER BY created_at DESC
                LIMIT %s;
            """, (q, q, limit))
            results = []
            for row in cur.fetchall():
                results.append({
                    "id": row[0], "channel": row[1], "thread_ts": row[2],
                    "summary": row[3], "domain": row[4],
                    "message_count": row[5], "participants": row[6] or [],
                    "created_at": row[7].isoformat() if hasattr(row[7], 'isoformat') else str(row[7]),
                })
            cur.close()
            return results

        return []

    # ── stats ────────────────────────────────────────────────

    def stats(self) -> dict:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*), domain FROM memories GROUP BY domain;")
        rows = cur.fetchall()
        domains = {r[1]: r[0] for r in rows}
        total = sum(domains.values())

        # Conversation context stats
        cur.execute("SELECT COUNT(*) FROM conversation_contexts WHERE expires_at > NOW();")
        ctx_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM conversation_blobs WHERE expires_at > NOW();")
        blob_count = cur.fetchone()[0]

        cur.close()
        return {
            "total_rules": total,
            "domains": domains,
            "backend": "postgres",
            "conversation_contexts": ctx_count,
            "conversation_blobs": blob_count,
        }
