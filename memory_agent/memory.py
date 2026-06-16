"""
Memory store — wraps Loom for rule lifecycle + pgvector for semantic search.
v1: file-based Loom RuleStore. pgvector activates when DATABASE_URL is set.
"""
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Memory:
    """A single memory/rule stored in Loom."""
    id: str
    domain: str
    rule_type: str
    rule: str
    example: str = ""
    confidence: int = 5
    sources: list = field(default_factory=list)


class MemoryStore:
    """Unified memory interface: Loom for lifecycle, pgvector for semantic search."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".loom-agent"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Loom RuleStore — always available (file-based for v1)
        from loom.engine.rule_store import RuleStore
        self._store = RuleStore(path=self.data_dir / "rules.json")

        # pgvector — only if DATABASE_URL is configured
        self._db_url = os.environ.get("DATABASE_URL") or os.environ.get("LOOM_DATABASE_URL")
        self._pg_store = None
        if self._db_url:
            self._init_postgres()

    def _init_postgres(self):
        """Lazy-init PostgresStore + pgvector when DB is available."""
        try:
            from loom.config import get_config
            from loom.storage.postgres_store import PostgresStore
            config = get_config()
            config.database_url = self._db_url
            self._pg_store = PostgresStore(config)
            self._pg_store.initialize()
            print(f"[memory] PostgresStore initialized", file=sys.stderr)
        except Exception as e:
            print(f"[memory] PostgresStore init failed — falling back to file store: {e}", file=sys.stderr)
            self._pg_store = None

    # ── session_init (recall) ──────────────────────────────

    def recall(self, query: str, domain: str | None = None,
               min_confidence: int = 1, limit: int = 10) -> list[Memory]:
        """
        Recall relevant memories for a query.
        Uses Loom's text search (substring match) for v1.
        When pgvector is active, runs semantic search instead.
        """
        if self._pg_store:
            # Postgres semantic search path (future)
            rules = self._pg_store.search_rules(
                query=query, domain=domain,
                min_confidence=min_confidence, limit=limit,
            )
        else:
            # File-based text search (v1)
            rules = self._store.search_rules(
                query=query, domain=domain,
                min_confidence=min_confidence, limit=limit,
            )

        return [
            Memory(
                id=r.id,
                domain=r.domain,
                rule_type=r.rule_type,
                rule=r.rule,
                example=getattr(r, 'example', ''),
                confidence=r.confidence,
                sources=getattr(r, 'sources', []),
            )
            for r in rules
        ]

    def get_context(self, task: str, role: str = "",
                    max_rules: int = 10) -> list[Memory]:
        """session_init — load all relevant context for a task."""
        memories = self.recall(query=task, min_confidence=3, limit=max_rules)

        # Fallback: if text search found few results, supplement with
        # high-confidence rules from general + coding domains
        if len(memories) < 3:
            for domain in ["coding", "general", "architecture", "security"]:
                domain_rules = self._store.search_rules(
                    query="", domain=domain, min_confidence=6, limit=5,
                )
                for r in domain_rules:
                    m = Memory(id=r.id, domain=r.domain, rule_type=r.rule_type,
                               rule=r.rule, confidence=r.confidence,
                               example=getattr(r, 'example', ''))
                    if m.id not in {mem.id for mem in memories}:
                        memories.append(m)
                if len(memories) >= max_rules:
                    break

        # Sort by confidence, highest first
        memories.sort(key=lambda m: m.confidence, reverse=True)
        return memories[:max_rules]

    # ── teach ──────────────────────────────────────────────

    def teach(self, domain: str, rule_type: str, rule: str,
              example: str = "", confidence: int = 7) -> Memory:
        """Store a new rule. Bumps confidence on duplicates."""
        r = self._store.add_rule(
            domain=domain,
            rule_type=rule_type,
            rule=rule.strip(),
            example=example,
            confidence=confidence,
            source_type="user_teach",
        )

        # Also write to Postgres if available
        if self._pg_store:
            try:
                self._pg_store.add_rule(
                    domain=domain, rule_type=rule_type,
                    rule=rule.strip(), example=example,
                    confidence=confidence, source_type="user_teach",
                )
            except Exception as e:
                print(f"[memory] Postgres teach failed (non-fatal): {e}", file=sys.stderr)

        return Memory(id=r.id, domain=r.domain, rule_type=r.rule_type,
                      rule=r.rule, confidence=r.confidence)

    # ── stats ──────────────────────────────────────────────

    def stats(self) -> dict:
        """Return memory store statistics."""
        all_rules = self._store.search_rules(query="", min_confidence=1)
        domains = {}
        for r in all_rules:
            domains[r.domain] = domains.get(r.domain, 0) + 1
        return {
            "total_rules": len(all_rules),
            "domains": domains,
            "backend": "postgres" if self._pg_store else "file",
        }
