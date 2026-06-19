"""Shared pytest fixtures for Loom tests."""

from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.config import LoomConfig

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set; skipping DB integration test",
)


@pytest.fixture
def fake_config() -> LoomConfig:
    """A minimal, valid config for unit tests (no external services)."""
    return LoomConfig(
        database_url=TEST_DATABASE_URL or "postgresql://localhost:5432/loom_test",
        env="test",
        llm_provider="skip",
        llm_api_key=None,
        embedding_provider="none",
        embedding_api_key=None,
        api_key=None,
        internal_api_token=None,
        gatekeeper_idle_seconds=1,
        gatekeeper_debounce_seconds=1,
        slack_buffer_max_messages=30,
    )


@pytest.fixture
async def db_pool(fake_config):
    """A real async DatabasePool. Skips if TEST_DATABASE_URL is unset."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from loom.db import DatabasePool

    config = replace(fake_config, database_url=TEST_DATABASE_URL)
    pool = DatabasePool(config, mode="async")
    await pool.init_async()
    # Clean slate for deterministic tests.
    await pool.execute("TRUNCATE memories, conversation_contexts, conversation_blobs")
    try:
        yield pool
    finally:
        await pool.close_async()


@pytest.fixture
def mock_embedding_service():
    """An embedding service whose embed() returns None by default."""
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def mock_llm_router():
    """An LLM router whose complete() returns a canned response."""
    from loom.llm.router import LLMResponse

    router = MagicMock()
    router.complete = AsyncMock(
        return_value=LLMResponse(text="ok", model_used="mock/model", usage=None)
    )
    return router
