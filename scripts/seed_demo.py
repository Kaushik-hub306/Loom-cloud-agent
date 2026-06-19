"""Seed Loom with a handful of demo memories.

Usage:
    LOOM_DATABASE_URL=postgresql://... python scripts/seed_demo.py
"""

from __future__ import annotations

import asyncio

import structlog

from loom.config import LoomConfig
from loom.db import DatabasePool
from loom.embeddings import EmbeddingService
from loom.memory.store import MemoryStore

logger = structlog.get_logger("loom.seed")

DEMO_MEMORIES = [
    ("coding", "convention", "Use async/await for all I/O operations.",
     "Use asyncpg instead of blocking psycopg2 in FastAPI routes.", 8),
    ("architecture", "decision", "Keep the Slack worker decoupled from the database.",
     "Slack talks to FastAPI over HTTP only.", 9),
    ("security", "policy", "Never log full API keys or tokens.",
     "Use redacted forms such as 'sk-123...<redacted>'.", 9),
    ("testing", "convention", "Integration tests must skip cleanly without a database.",
     "Guard DB tests with a TEST_DATABASE_URL check.", 7),
]


async def main() -> None:
    config = LoomConfig.from_env()
    pool = DatabasePool(config, mode="async")
    await pool.init_async()
    try:
        store = MemoryStore(pool, EmbeddingService(config), config)
        for domain, rule_type, rule, example, confidence in DEMO_MEMORIES:
            result = await store.teach(
                domain, rule_type, rule, example=example, confidence=confidence
            )
            logger.info(
                "seeded", memory_id=result.memory.id, is_update=result.is_update
            )
    finally:
        await pool.close_async()


if __name__ == "__main__":
    asyncio.run(main())
