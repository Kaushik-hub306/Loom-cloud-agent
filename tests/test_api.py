"""Phase 4 acceptance tests for the Loom FastAPI memory service.

These drive the real app via ``httpx.ASGITransport`` so the lifespan runs and
the real ``DatabasePool``/``MemoryStore`` are exercised against the test DB.
External boundaries (LLM, DB health) are swapped on ``app.state`` after startup.

All DB-touching tests are guarded by ``requires_db`` (mirrors
``tests/conftest.py``); without ``TEST_DATABASE_URL`` they skip cleanly.

API-specific fixtures/helpers are defined locally here, per the Phase 4 brief.
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from loom.api.app import create_app
from loom.config import LoomConfig
from loom.llm.router import LLMResponse

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set; skipping DB integration test",
)


def _make_config(**overrides) -> LoomConfig:
    base = dict(
        database_url=TEST_DATABASE_URL or "postgresql://localhost:5432/loom_test",
        env="test",
        llm_provider="skip",
        llm_api_key=None,
        embedding_provider="none",
        embedding_api_key=None,
        api_key=None,
        internal_api_token=None,
    )
    base.update(overrides)
    return LoomConfig(**base)


@contextlib.asynccontextmanager
async def _client(config: LoomConfig):
    """Build the app, run its lifespan, truncate tables, yield (app, client)."""
    app = create_app(config)
    async with app.router.lifespan_context(app):
        await app.state.pool.execute(
            "TRUNCATE memories, conversation_contexts, conversation_blobs"
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield app, client


async def _teach(client: httpx.AsyncClient, **overrides) -> httpx.Response:
    payload = {
        "domain": "coding",
        "rule_type": "convention",
        "rule": "Use async/await for all I/O operations.",
        "example": "Use asyncpg instead of blocking psycopg2 in FastAPI routes.",
        "confidence": 7,
    }
    payload.update(overrides)
    return await client.post("/teach", json=payload)


# ---------------------------------------------------------------------------
# session_init
# ---------------------------------------------------------------------------
@requires_db
async def test_session_init_returns_memories_and_contexts():
    async with _client(_make_config()) as (app, client):
        await _teach(client)
        await app.state.store.save_context_summary(
            channel="eng",
            thread_ts="100.1",
            summary="Decided auth middleware should stay framework-agnostic.",
            domain="architecture",
        )

        resp = await client.post(
            "/session_init", json={"task": "auth middleware refactor", "channel": "eng"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_count"] >= 1
        assert body["context_count"] >= 1
        assert "<!-- LOOM:SESSION_CONTEXT -->" in body["context_prompt"]
        assert len(body["memories"]) == body["memory_count"]
        assert len(body["contexts"]) == body["context_count"]


@requires_db
async def test_session_init_is_read_only():
    async with _client(_make_config()) as (app, client):
        # Seed one context, then confirm session_init does not add another.
        await app.state.store.save_context_summary(
            channel="eng",
            thread_ts="200.1",
            summary="A durable architectural decision worth keeping around.",
            domain="architecture",
        )
        before = await app.state.pool.fetchval(
            "SELECT COUNT(*) FROM conversation_contexts"
        )
        resp = await client.post("/session_init", json={"task": "anything", "channel": "eng"})
        assert resp.status_code == 200
        after = await app.state.pool.fetchval(
            "SELECT COUNT(*) FROM conversation_contexts"
        )
        assert before == after == 1


@requires_db
async def test_session_init_with_empty_store_returns_empty_gracefully():
    async with _client(_make_config()) as (_app, client):
        resp = await client.post("/session_init", json={"task": "fresh start"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_count"] == 0
        assert body["context_count"] == 0
        assert body["memories"] == []
        assert body["contexts"] == []
        assert "0 rules | 0 past conversations" in body["context_prompt"]


# ---------------------------------------------------------------------------
# teach
# ---------------------------------------------------------------------------
@requires_db
async def test_teach_creates_new_rule():
    async with _client(_make_config()) as (_app, client):
        resp = await _teach(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_update"] is False
        assert body["domain"] == "coding"
        assert body["message"] == "Remembered coding convention."
        assert body["id"]


@requires_db
async def test_teach_duplicate_returns_is_update_true():
    async with _client(_make_config()) as (_app, client):
        first = await _teach(client)
        assert first.json()["is_update"] is False
        second = await _teach(client)
        body = second.json()
        assert body["is_update"] is True
        assert "Updated existing" in body["message"]
        assert body["confidence"] == 8  # min(10, max(7,7)+1)


@requires_db
async def test_teach_validates_min_rule_length():
    async with _client(_make_config()) as (_app, client):
        resp = await _teach(client, rule="hi")  # under 5 chars
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------
@requires_db
async def test_recall_route_returns_memories_and_contexts():
    async with _client(_make_config()) as (app, client):
        await _teach(client)
        await app.state.store.save_context_summary(
            channel="eng",
            thread_ts="300.1",
            summary="Use async database access everywhere in the service layer.",
            domain="coding",
        )
        resp = await client.post(
            "/recall", json={"query": "async I/O", "include_contexts": True}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_count"] >= 1
        assert body["context_count"] >= 1


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------
@requires_db
async def test_ask_calls_llm_with_memory_context():
    config = _make_config(llm_provider="deepseek", llm_api_key="test-key")
    async with _client(config) as (app, client):
        await _teach(client)
        mock_router = MagicMock()
        mock_router.complete = AsyncMock(
            return_value=LLMResponse(
                text="Here is the answer.",
                model_used="mock/model",
                usage={"total_tokens": 5},
            )
        )
        app.state.llm_router = mock_router

        resp = await client.post(
            "/ask",
            json={"task": "async I/O", "message": "How should I do database calls?"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["response"] == "Here is the answer."
        assert body["model_used"] == "mock/model"
        assert body["memories_used"] >= 1

        assert mock_router.complete.await_count == 1
        user_prompt = mock_router.complete.await_args.kwargs["user"]
        assert "<!-- LOOM:SESSION_CONTEXT -->" in user_prompt
        assert "async/await" in user_prompt


@requires_db
async def test_ask_returns_503_when_llm_provider_skip():
    async with _client(_make_config(llm_provider="skip")) as (_app, client):
        resp = await client.post(
            "/ask", json={"task": "anything", "message": "hello"}
        )
        assert resp.status_code == 503
        assert resp.json()["error"] == "llm_error"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
@requires_db
async def test_health_returns_ok_when_db_connected():
    async with _client(_make_config()) as (_app, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["database"]["connected"] is True
        assert body["version"]
        assert body["uptime_seconds"] >= 0


@requires_db
async def test_health_returns_degraded_on_slow_db():
    async with _client(_make_config()) as (app, client):
        app.state.pool.health_check_async = AsyncMock(
            return_value={"connected": True, "latency_ms": 999.0}
        )
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"


@requires_db
async def test_health_returns_error_when_db_down():
    async with _client(_make_config()) as (app, client):
        app.state.pool.health_check_async = AsyncMock(
            return_value={"connected": False, "latency_ms": None}
        )
        resp = await client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "error"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
@requires_db
async def test_stats_returns_correct_structure():
    async with _client(_make_config()) as (_app, client):
        await _teach(client)
        resp = await client.get("/stats")
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "total_rules",
            "rules_by_domain",
            "rules_by_type",
            "total_uses",
            "active_contexts",
            "active_blobs",
            "expired_contexts",
            "expired_blobs",
        ):
            assert key in body
        assert body["total_rules"] == 1


# ---------------------------------------------------------------------------
# internal routes
# ---------------------------------------------------------------------------
@requires_db
async def test_internal_blob_endpoint_requires_internal_token():
    config = _make_config(internal_api_token="secret-internal")
    async with _client(config) as (_app, client):
        payload = {
            "channel": "eng",
            "thread_ts": "400.1",
            "messages": [{"user": "u1", "text": "hi", "ts": "400.1"}],
        }
        no_token = await client.post("/internal/conversation_blob", json=payload)
        assert no_token.status_code == 401

        ok = await client.post(
            "/internal/conversation_blob",
            json=payload,
            headers={"X-Loom-Internal-Token": "secret-internal"},
        )
        assert ok.status_code == 200
        assert ok.json()["saved"] is True


@requires_db
async def test_internal_context_endpoint_saves_new_topic():
    config = _make_config(internal_api_token="secret-internal")
    async with _client(config) as (app, client):
        headers = {"X-Loom-Internal-Token": "secret-internal"}
        body = {
            "channel": "eng",
            "thread_ts": "500.1",
            "summary": "First durable topic summary for this thread to keep.",
            "domain": "coding",
        }
        first = await client.post("/internal/context_summary", json=body, headers=headers)
        assert first.status_code == 200
        assert first.json()["saved"] is True

        body2 = dict(body)
        body2["summary"] = "A clearly different second topic worth preserving too."
        body2["is_new_topic"] = True
        second = await client.post(
            "/internal/context_summary", json=body2, headers=headers
        )
        assert second.status_code == 200
        assert second.json()["saved"] is True
        assert second.json()["id"] != first.json()["id"]

        count = await app.state.pool.fetchval(
            "SELECT COUNT(*) FROM conversation_contexts WHERE thread_ts = $1", "500.1"
        )
        assert count == 2


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
@requires_db
async def test_api_key_required_when_configured():
    config = _make_config(api_key="public-key")
    async with _client(config) as (_app, client):
        denied = await _teach(client)
        assert denied.status_code == 401
        assert denied.json()["error"] == "api_auth_error"

        allowed = await client.post(
            "/teach",
            json={
                "domain": "coding",
                "rule_type": "convention",
                "rule": "Use async/await for all I/O operations.",
            },
            headers={"X-Loom-Api-Key": "public-key"},
        )
        assert allowed.status_code == 200


# ---------------------------------------------------------------------------
# error handling + request id
# ---------------------------------------------------------------------------
@requires_db
async def test_global_error_handler_does_not_leak_stacktrace():
    async with _client(_make_config()) as (app, client):
        broken_store = MagicMock()
        broken_store.stats = AsyncMock(side_effect=ValueError("kaboom secret detail"))
        app.state.store = broken_store

        resp = await client.get("/stats")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "internal_error"
        assert "request_id" in body
        # No stack trace / internal detail leaked.
        text = resp.text
        assert "Traceback" not in text
        assert "kaboom" not in text
        assert "ValueError" not in text


@requires_db
async def test_request_id_header_present_on_all_responses():
    async with _client(_make_config()) as (_app, client):
        health = await client.get("/health")
        assert "X-Request-ID" in health.headers

        echoed = await client.get("/health", headers={"X-Request-ID": "custom-rid-123"})
        assert echoed.headers["X-Request-ID"] == "custom-rid-123"

        invalid = await client.post("/teach", json={})
        assert invalid.status_code == 422
        assert "X-Request-ID" in invalid.headers


# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------
@requires_db
async def test_export_import_roundtrip():
    async with _client(_make_config()) as (app, client):
        await _teach(client)
        export = await client.get("/export")
        assert export.status_code == 200
        payload = export.json()
        assert payload["version"]
        assert len(payload["memories"]) == 1

        # Wipe and re-import.
        await app.state.pool.execute(
            "TRUNCATE memories, conversation_contexts, conversation_blobs"
        )
        imported = await client.post(
            "/import?regenerate_embeddings=true", json=payload
        )
        assert imported.status_code == 200
        counts = imported.json()
        assert counts["imported"] == 1
        assert counts["failed"] == 0

        stats = await client.get("/stats")
        assert stats.json()["total_rules"] == 1
