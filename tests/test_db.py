"""Phase 1 database tests. Require TEST_DATABASE_URL (integration)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from loom.db import DatabasePool
from loom.errors import LoomDBError
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = pytest.mark.integration


@requires_db
async def test_pool_connects_and_health_check_returns_latency(db_pool):
    health = await db_pool.health_check_async()
    assert health["connected"] is True
    assert isinstance(health["latency_ms"], float)


@requires_db
async def test_migrations_are_idempotent(db_pool):
    # Running again must not raise.
    await db_pool.run_migrations_async()
    await db_pool.run_migrations_async()


@requires_db
async def test_pool_raises_loom_db_error_on_bad_query(db_pool):
    with pytest.raises(LoomDBError):
        await db_pool.fetch("SELECT * FROM table_that_does_not_exist")


@requires_db
async def test_pgvector_extension_is_enabled(db_pool):
    row = await db_pool.fetchrow(
        "SELECT 1 AS ok FROM pg_extension WHERE extname = 'vector'"
    )
    assert row is not None


@requires_db
async def test_all_tables_exist_after_migration(db_pool):
    rows = await db_pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    )
    names = {r["table_name"] for r in rows}
    assert {"memories", "conversation_contexts", "conversation_blobs",
            "schema_migrations"}.issubset(names)


@requires_db
async def test_contexts_primary_key_allows_multiple_topics_per_thread(db_pool):
    base = dict(channel="C1", thread_ts="100.1", summary="a summary long enough")
    await db_pool.execute(
        "INSERT INTO conversation_contexts (id, workspace_id, channel, thread_ts, "
        "topic_index, summary) VALUES ($1,'',$2,$3,$4,$5)",
        "id-0", base["channel"], base["thread_ts"], 0, "first summary text here",
    )
    await db_pool.execute(
        "INSERT INTO conversation_contexts (id, workspace_id, channel, thread_ts, "
        "topic_index, summary) VALUES ($1,'',$2,$3,$4,$5)",
        "id-1", base["channel"], base["thread_ts"], 1, "second summary text here",
    )
    rows = await db_pool.fetch(
        "SELECT topic_index FROM conversation_contexts WHERE channel=$1 AND thread_ts=$2",
        base["channel"], base["thread_ts"],
    )
    assert {r["topic_index"] for r in rows} == {0, 1}


@requires_db
def test_sync_pool_connects_for_mcp(fake_config):
    config = replace(fake_config, database_url=TEST_DATABASE_URL)
    pool = DatabasePool(config, mode="sync")
    pool.init_sync()
    try:
        health = pool.health_check_sync()
        assert health["connected"] is True
    finally:
        pool.close_sync()
