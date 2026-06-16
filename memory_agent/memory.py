"""
Memory store — shared storage with semantic search.
Postgres + pgvector when DATABASE_URL is set. JSON file fallback.
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
    """Shared memory: Postgres + pgvector (primary) or JSON file (fallback)."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".loom-agent"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._db_url = os.environ.get("DATABASE_URL") or os.environ.get("LOOM_DATABASE_URL")
        self._conn = None  # psycopg2 connection (Postgres path)
        self._file_store = None  # Loom RuleStore (fallback path)
        self._backend = "file"

        if self._db_url:
            try:
                self._conn = psycopg2.connect(self._db_url)
                self._conn.autocommit = True
                self._backend = "postgres"
            except Exception as e:
                print(f"[memory] Postgres unavailable — using file store: {e}", file=sys.stderr)

        if self._backend == "file":
            from loom.engine.rule_store import RuleStore
            self._file_store = RuleStore(path=self.data_dir / "rules.json")

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
        """Recall relevant memories. Postgres: semantic. File: substring."""

        if self._conn:
            return self._recall_postgres(query, domain, min_confidence, limit)
        else:
            return self._recall_file(query, domain, min_confidence, limit)

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
                FROM rules
                WHERE embedding IS NOT NULL
                  AND confidence >= %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
            """, (json.dumps(embedding), min_confidence, json.dumps(embedding), limit))
        else:
            # Fallback: text search
            cur.execute("""
                SELECT id, domain, rule_type, rule, example, confidence, sources, 0.0
                FROM rules
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

    def _recall_file(self, query: str, domain: str | None,
                     min_confidence: int, limit: int) -> list[Memory]:
        """Substring search via Loom RuleStore."""
        rules = self._file_store.search_rules(
            query=query, domain=domain,
            min_confidence=min_confidence, limit=limit,
        )
        return [
            Memory(id=r.id, domain=r.domain, rule_type=r.rule_type,
                   rule=r.rule, example=getattr(r, 'example', ''),
                   confidence=r.confidence, sources=getattr(r, 'sources', []))
            for r in rules
        ]

    # ── context / session_init ───────────────────────────────

    def get_context(self, task: str, role: str = "",
                    max_rules: int = 10) -> list[Memory]:
        """Load all relevant context for a task."""
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

    # ── teach ────────────────────────────────────────────────

    def teach(self, domain: str, rule_type: str, rule: str,
              example: str = "", confidence: int = 7) -> Memory:
        """Store a new rule with embedding for semantic search."""

        if self._conn:
            return self._teach_postgres(domain, rule_type, rule, example, confidence)
        else:
            return self._teach_file(domain, rule_type, rule, example, confidence)

    def _teach_postgres(self, domain: str, rule_type: str, rule: str,
                        example: str, confidence: int) -> Memory:
        """Store in Postgres with embedding."""
        rule_id = f"{domain}::{rule_type}::{rule.lower().replace(' ', '-')[:80]}"
        embedding = self._embed(rule)

        cur = self._conn.cursor()

        # Upsert: insert or bump confidence
        cur.execute("""
            INSERT INTO rules (id, domain, rule_type, rule, example, confidence,
                               sources, source_type, embedding, project)
            VALUES (%s, %s, %s, %s, %s, %s, '[]', 'user_teach', %s::vector, 'loom-agent')
            ON CONFLICT (id) DO UPDATE SET
                confidence = LEAST(10, rules.confidence + 1),
                updated_at = NOW(),
                embedding = COALESCE(EXCLUDED.embedding, rules.embedding);
        """, (rule_id, domain, rule_type, rule.strip(), example, confidence,
              json.dumps(embedding) if embedding else None))

        # Get final state
        cur.execute("SELECT id, confidence FROM rules WHERE id = %s;", (rule_id,))
        row = cur.fetchone()
        cur.close()

        stored_confidence = row[1] if row else confidence
        return Memory(id=rule_id, domain=domain, rule_type=rule_type,
                      rule=rule.strip(), confidence=stored_confidence)

    def _teach_file(self, domain: str, rule_type: str, rule: str,
                    example: str, confidence: int) -> Memory:
        """Store in file-based Loom store."""
        r = self._file_store.add_rule(
            domain=domain, rule_type=rule_type, rule=rule.strip(),
            example=example, confidence=confidence, source_type="user_teach",
        )
        return Memory(id=r.id, domain=r.domain, rule_type=r.rule_type,
                      rule=r.rule, confidence=r.confidence)

    # ── stats ────────────────────────────────────────────────

    def stats(self) -> dict:
        if self._conn:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*), domain FROM rules GROUP BY domain;")
            rows = cur.fetchall()
            cur.close()
            domains = {r[1]: r[0] for r in rows}
            total = sum(domains.values())
            return {"total_rules": total, "domains": domains, "backend": "postgres"}

        all_rules = self._file_store.search_rules(query="", min_confidence=1)
        domains = {}
        for r in all_rules:
            domains[r.domain] = domains.get(r.domain, 0) + 1
        return {"total_rules": len(all_rules), "domains": domains, "backend": "file"}
