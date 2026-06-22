"""Phase 2 memory store tests (integration: require TEST_DATABASE_URL)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.memory.store import MemoryStore
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


class ConstEmbeddings:
    """Returns a constant non-null vector for any non-empty text."""

    def __init__(self, dimension: int = 768):
        self.dimension = dimension

    async def embed(self, text: str):
        if not text or not text.strip():
            return None
        return [0.1] * self.dimension


class NoEmbeddings:
    async def embed(self, text: str):
        return None


def make_store(db_pool, embeddings):
    return MemoryStore(db_pool, embeddings, db_pool.config)


async def test_teach_stores_rule_and_returns_memory(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    result = await store.teach(
        "coding", "convention", "Use async/await for all I/O operations",
        example="Use asyncpg in FastAPI routes.", confidence=7,
    )
    assert result.is_update is False
    assert result.memory.domain == "coding"
    assert result.memory.confidence == 7
    assert result.memory.rule.startswith("Use async/await")


async def test_teach_duplicate_bumps_confidence_not_duplicates(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    r1 = await store.teach("coding", "convention", "Always write tests first", confidence=5)
    r2 = await store.teach("coding", "convention", "Always write tests first", confidence=5)
    assert r1.memory.id == r2.memory.id
    assert r2.is_update is True
    assert r2.memory.confidence == 6  # min(10, max(5,5)+1)
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM memories WHERE id = $1", r1.memory.id
    )
    assert count == 1


async def test_teach_concurrent_duplicates_are_atomic(db_pool):
    store = make_store(db_pool, NoEmbeddings())

    results = await asyncio.gather(
        *[
            store.teach(
                "coding",
                "convention",
                "Concurrent teaches should upsert atomically",
                confidence=5,
            )
            for _ in range(8)
        ]
    )

    memory_ids = {result.memory.id for result in results}
    assert len(memory_ids) == 1
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM memories WHERE id = $1", next(iter(memory_ids))
    )
    confidence = await db_pool.fetchval(
        "SELECT confidence FROM memories WHERE id = $1", next(iter(memory_ids))
    )
    assert count == 1
    assert confidence == 10


async def test_teach_confidence_capped_at_10(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    last = None
    for _ in range(15):
        last = await store.teach("coding", "convention", "Cap confidence rule here", confidence=10)
    assert last.memory.confidence == 10


async def test_teach_generates_stable_slug_id(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    result = await store.teach("Coding", "Convention", "Use async/await for I/O.", confidence=7)
    assert result.memory.id == "coding::convention::use-async-await-for-i-o"


async def test_recall_returns_relevant_rules_semantic(db_pool):
    store = make_store(db_pool, ConstEmbeddings())
    await store.teach("coding", "convention", "Use dependency injection for services", confidence=8)
    results = await store.recall("dependency injection", limit=5)
    assert any("dependency injection" in m.rule.lower() for m in results)


async def test_recall_falls_back_to_text_search_without_embedding(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    await store.teach(
        "security", "policy", "Validate all webhook signatures carefully", confidence=7
    )
    results = await store.recall("webhook signatures", limit=5)
    assert any("webhook" in m.rule.lower() for m in results)


async def test_recall_final_fallback_returns_high_confidence_rules(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    await store.teach("coding", "convention", "Prefer composition over inheritance", confidence=9)
    results = await store.recall("zzzqqq nonexistent gibberish term", limit=5)
    assert len(results) >= 1


async def test_recall_never_raises_on_db_error(db_pool):
    broken = MagicMock()
    broken.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    store = MemoryStore(broken, NoEmbeddings(), db_pool.config)
    results = await store.recall("anything")
    assert results == []


async def test_recall_increments_uses_with_logged_task(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    r = await store.teach(
        "testing", "convention", "Use deterministic fixtures always", confidence=7
    )
    await store.recall("deterministic fixtures", limit=5)
    for _ in range(20):
        await asyncio.sleep(0.05)
        uses = await db_pool.fetchval("SELECT uses FROM memories WHERE id=$1", r.memory.id)
        if uses and uses >= 1:
            break
    assert uses >= 1


async def test_get_session_context_runs_concurrently(db_pool):
    store = make_store(db_pool, ConstEmbeddings())
    await store.teach("coding", "convention", "Keep functions small and focused", confidence=8)

    order: list[str] = []

    async def slow_recall(*args, **kwargs):
        order.append("recall_start")
        await asyncio.sleep(0.1)
        order.append("recall_end")
        return []

    async def slow_search(*args, **kwargs):
        order.append("search_start")
        await asyncio.sleep(0.1)
        order.append("search_end")
        return []

    store.recall = slow_recall  # type: ignore[assignment]
    store.search_contexts = slow_search  # type: ignore[assignment]

    loop = asyncio.get_event_loop()
    start = loop.time()
    await store.get_session_context("a task", channel="general")
    elapsed = loop.time() - start
    # If concurrent, both 0.1s sleeps overlap -> well under 0.2s.
    assert elapsed < 0.18
    assert order[0] == "recall_start"
    assert "search_start" in order[:2]


async def test_get_session_context_can_disable_contexts(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    called = {"search": False}

    async def search(*args, **kwargs):
        called["search"] = True
        return []

    store.search_contexts = search  # type: ignore[assignment]
    ctx = await store.get_session_context("task", include_contexts=False)
    assert ctx.contexts == []
    assert called["search"] is False


async def test_save_blob_sanitizes_message_payloads(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    messages = [
        {"user": "U1", "text": "hi", "ts": "1", "thread_ts": "1", "is_bot": False,
         "secret": "should-be-dropped", "blocks": [1, 2, 3]},
    ]
    blob_id = await store.save_conversation_blob("C1", "1", messages)
    assert blob_id
    row = await db_pool.fetchrow("SELECT messages FROM conversation_blobs WHERE id=$1", blob_id)
    import json
    stored = json.loads(row["messages"]) if isinstance(row["messages"], str) else row["messages"]
    assert set(stored[0].keys()) == {"user", "text", "ts", "thread_ts", "is_bot"}


async def test_save_context_truncates_to_500_chars(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    long_summary = "x" * 800
    ctx_id = await store.save_context_summary("C1", "10.1", long_summary)
    assert ctx_id
    summary = await db_pool.fetchval(
        "SELECT summary FROM conversation_contexts WHERE id=$1", ctx_id
    )
    assert len(summary) == 500


async def test_save_context_new_topic_preserves_existing_summary(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    id0 = await store.save_context_summary("C1", "20.1", "first topic summary here")
    id1 = await store.save_context_summary(
        "C1", "20.1", "second distinct topic summary", is_new_topic=True
    )
    assert id0 != id1
    rows = await db_pool.fetch(
        "SELECT topic_index, summary FROM conversation_contexts WHERE channel='C1' "
        "AND thread_ts='20.1' ORDER BY topic_index"
    )
    assert [r["topic_index"] for r in rows] == [0, 1]


async def test_save_context_concurrent_new_topics_keep_distinct_rows(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    summaries = [f"distinct topic summary number {idx}" for idx in range(8)]

    ids = await asyncio.gather(
        *[
            store.save_context_summary("C2", "30.1", summary, is_new_topic=True)
            for summary in summaries
        ]
    )

    rows = await db_pool.fetch(
        "SELECT topic_index, summary FROM conversation_contexts WHERE channel='C2' "
        "AND thread_ts='30.1' ORDER BY topic_index"
    )
    assert len(set(ids)) == len(summaries)
    assert [r["topic_index"] for r in rows] == list(range(len(summaries)))
    assert {r["summary"] for r in rows} == set(summaries)


async def test_search_contexts_applies_recency_weighting(db_pool):
    store = make_store(db_pool, ConstEmbeddings())
    new_id = await store.save_context_summary("C9", "1.1", "recent relevant summary text")
    old_id = await store.save_context_summary("C9", "2.2", "older relevant summary text")
    # Backdate the old one. The BEFORE UPDATE trigger forces updated_at=NOW(),
    # so disable it for this manual backdate.
    await db_pool.execute(
        "ALTER TABLE conversation_contexts DISABLE TRIGGER trg_contexts_updated_at"
    )
    await db_pool.execute(
        "UPDATE conversation_contexts SET updated_at = NOW() - INTERVAL '60 days' WHERE id=$1",
        old_id,
    )
    await db_pool.execute(
        "ALTER TABLE conversation_contexts ENABLE TRIGGER trg_contexts_updated_at"
    )
    results = await store.search_contexts("relevant summary", channel="C9", limit=5)
    ids = [c.id for c in results]
    assert ids.index(new_id) < ids.index(old_id)


async def test_stats_returns_correct_counts(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    await store.teach("coding", "convention", "Stats rule number one here", confidence=7)
    await store.teach("security", "policy", "Stats rule number two here", confidence=8)
    await store.save_context_summary("C1", "1.1", "a context summary for stats")
    await store.save_conversation_blob("C1", "1.1", [{"user": "U", "text": "hi", "ts": "1"}])
    stats = await store.stats()
    assert stats.total_rules == 2
    assert stats.rules_by_domain.get("coding") == 1
    assert stats.active_contexts == 1
    assert stats.active_blobs == 1


async def test_export_import_roundtrip_preserves_fields(db_pool):
    store = make_store(db_pool, NoEmbeddings())
    await store.teach(
        "coding", "convention", "Roundtrip rule for export tests",
        example="An example here", confidence=8,
    )
    export = await store.export_memories()
    assert export["version"] == "1.0.0"
    assert len(export["memories"]) == 1

    await db_pool.execute("TRUNCATE memories")
    result = await store.import_memories(export, regenerate_embeddings=False)
    assert result["imported"] == 1

    rows = await db_pool.fetch("SELECT * FROM memories")
    assert len(rows) == 1
    assert rows[0]["rule"] == "Roundtrip rule for export tests"
    assert rows[0]["example"] == "An example here"
    assert rows[0]["confidence"] == 8


async def test_import_regenerates_embeddings_when_requested(db_pool):
    store = make_store(db_pool, ConstEmbeddings())
    payload = {
        "version": "1.0.0",
        "exported_at": "2026-06-19T00:00:00Z",
        "include_embeddings": False,
        "memories": [
            {
                "id": "ignored",
                "domain": "coding",
                "rule_type": "convention",
                "rule": "Regenerate embeddings on import here",
                "example": "",
                "confidence": 7,
                "uses": 0,
                "sources": [],
                "source_type": "import",
                "project": "default",
            }
        ],
    }
    result = await store.import_memories(payload, regenerate_embeddings=True)
    assert result["imported"] == 1
    has_embedding = await db_pool.fetchval(
        "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL"
    )
    assert has_embedding == 1
