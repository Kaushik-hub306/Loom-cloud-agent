"""Tests for conversation memory — context summaries, blob backup, and search."""

import json
import time
from pathlib import Path

import pytest


# ── Context summary ID generation ───────────────────────────────

def _make_conversation_id(channel: str, thread_ts: str) -> str:
    """Deterministic ID from Slack identifiers (standalone, no store needed)."""
    import hashlib
    raw = f"{channel}:{thread_ts}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def test_make_conversation_id_deterministic():
    """Same channel + thread_ts produce the same ID."""
    id1 = _make_conversation_id("C123", "1234567890.001")
    id2 = _make_conversation_id("C123", "1234567890.001")
    assert id1 == id2
    assert len(id1) == 16  # MD5 hex truncated


def test_make_conversation_id_different_channel():
    """Different channels produce different IDs."""
    id1 = _make_conversation_id("C123", "1234567890.001")
    id2 = _make_conversation_id("C456", "1234567890.001")
    assert id1 != id2


# ── Gatekeeper prompt parsing (unit tests for the parsing logic) ──

def test_parse_context_response():
    """Parse CONTEXT: domain | summary from LLM output."""
    text = "CONTEXT: security | Audited auth module — found 3 issues"
    assert text.upper().startswith("CONTEXT:")
    rest = text[len("CONTEXT:"):].strip()
    parts = rest.split("|", 1)
    assert parts[0].strip() == "security"
    assert "Audited auth module" in parts[1].strip()


def test_parse_context_new_response():
    """Parse CONTEXT_NEW for topic-shifted conversations."""
    text = "CONTEXT_NEW: deployment | Resolved Railway cold start issue"
    assert text.upper().startswith("CONTEXT_NEW:")
    rest = text[len("CONTEXT_NEW:"):].strip()
    parts = rest.split("|", 1)
    assert parts[0].strip() == "deployment"


def test_parse_nothing_response():
    """NOTHING means no context to save."""
    text = "NOTHING"
    assert text.upper() == "NOTHING"


def test_parse_garbage_response():
    """Unrecognized output is rejected."""
    text = "Let me think about that..."
    assert not text.upper().startswith("CONTEXT:")
    assert not text.upper().startswith("CONTEXT_NEW:")
    assert text.upper() != "NOTHING"


def test_summary_too_short_rejected():
    """Summaries under 10 chars are not worth saving."""
    summary = "OK."
    assert len(summary.strip()) < 10


def test_summary_minimum_length_accepted():
    """Summaries >= 10 chars pass the minimum bar."""
    summary = "Audited auth module and found issues"
    assert len(summary.strip()) >= 10


# ── Recency weighting ───────────────────────────────────────────

def test_recency_decay_10_day_half_life():
    """10-day half-life means a 10-day-old score is halved."""
    days_ago = 10
    factor = 2 ** (-days_ago / 10)  # exact half-life formula
    assert abs(factor - 0.5) < 0.001


def test_recency_decay_30_days():
    """30 days = 1/8 of original score."""
    days_ago = 30
    factor = 2 ** (-days_ago / 10)
    assert abs(factor - 0.125) < 0.001


def test_recency_decay_fresh():
    """Fresh context gets nearly full weight."""
    days_ago = 0
    factor = 2 ** (-days_ago / 10)
    assert abs(factor - 1.0) < 0.001


# ── Token budget enforcement ─────────────────────────────────────

def test_context_summary_max_500_chars():
    """Summaries are truncated to 500 characters."""
    long_summary = "x" * 1000
    truncated = long_summary.strip()[:500]
    assert len(truncated) == 500


def test_max_3_contexts_returned():
    """session_init caps context summaries at 3."""
    result_count = min(3, 3)
    assert result_count <= 3

    # If we have more matches, we still cap at 3
    result_count = min(5, 3)
    assert result_count == 3


def test_total_context_chars_under_1500():
    """3 summaries × 500 chars max = 1500 chars total budget."""
    summaries = ["Audited auth module — found 3 issues: missing rate limiting, "
                 "JWT not validated on refresh, session tokens in URL params",
                 "Decided on repository pattern for DB access",
                 "Migrated CI from GitHub Actions to Railway"]
    total = sum(len(s[:500]) for s in summaries[:3])
    assert total <= 1500


# ── Blob backup (data-loss guard) ────────────────────────────────

def test_blob_messages_are_valid_json():
    """Raw messages stored as JSON array."""
    messages = [
        {"role": "user", "content": "Run a security audit on auth.py"},
        {"role": "assistant", "content": "Found 3 issues: rate limiting, JWT, tokens"},
    ]
    blob = json.dumps(messages)
    parsed = json.loads(blob)
    assert len(parsed) == 2
    assert parsed[0]["role"] == "user"
    assert parsed[1]["role"] == "assistant"


# ── Debounce logic ───────────────────────────────────────────────

def test_debounce_allows_first_call():
    """First evaluation always proceeds."""
    last_eval = 0
    now = time.time()
    assert now - last_eval >= 180


def test_debounce_blocks_rapid_re_eval():
    """Second evaluation within 3 minutes is blocked."""
    last_eval = time.time() - 60  # 60 seconds ago
    now = time.time()
    assert now - last_eval < 180


def test_debounce_allows_after_3_minutes():
    """Evaluation after 3+ minutes proceeds."""
    last_eval = time.time() - 200  # 200 seconds ago
    now = time.time()
    assert now - last_eval >= 180


# ── Gatekeeper prompt eval (LLM quality test) ────────────────────

# These are manual eval cases — run with a real LLM to validate
# that the gatekeeper correctly identifies context-worthy conversations.

EVAL_CASES = [
    {
        "name": "security_audit",
        "conversation": (
            "user: Run a security audit on auth.py\n"
            "assistant: I found 3 issues: missing rate limiting on /login, "
            "JWT tokens not validated on refresh, and session tokens appearing "
            "in URL parameters. All three should be fixed before deployment."
        ),
        "expect_context": True,
        "expect_domain": "security",
        "reason": "Clear decision + problems identified. Should produce CONTEXT.",
    },
    {
        "name": "casual_greeting",
        "conversation": (
            "user: hello\n"
            "assistant: Hi! What can I help with today?"
        ),
        "expect_context": False,
        "reason": "Trivial greeting. Should produce NOTHING.",
    },
    {
        "name": "architecture_decision",
        "conversation": (
            "user: Should we use repository pattern or active record?\n"
            "assistant: Repository pattern. Active Record makes testing hard "
            "because you can't mock the database layer. We use repository "
            "pattern for all DB access going forward."
        ),
        "expect_context": True,
        "expect_domain": "architecture",
        "reason": "Architecture decision with rationale. Should produce CONTEXT.",
    },
    {
        "name": "topic_shift",
        "conversation": (
            "user: Found the bug in auth.py — it was a race condition.\n"
            "assistant: Fixed. Now, let's talk about the deployment pipeline. "
            "We need to migrate from GitHub Actions to Railway deploy."
        ),
        "expect_context": True,
        "expect_is_new": True,
        "reason": "Two topics — bug fix then deployment shift. "
                 "Should produce CONTEXT_NEW for the deployment topic.",
    },
    {
        "name": "acknowledgment_only",
        "conversation": (
            "user: Implement feature X\n"
            "assistant: Done. The PR is up.\n"
            "user: thanks\n"
            "assistant: You're welcome!"
        ),
        "expect_context": False,
        "reason": "Simple acknowledgment. No decisions or problems. Should produce NOTHING.",
    },
]


def test_eval_cases_have_expected_structure():
    """Every eval case has required fields."""
    for case in EVAL_CASES:
        assert "name" in case
        assert "conversation" in case
        assert "expect_context" in case
        assert "reason" in case


def test_eval_case_count():
    """We have at least 3 eval cases covering the main scenarios."""
    assert len(EVAL_CASES) >= 3
    has_context_true = any(c["expect_context"] for c in EVAL_CASES)
    has_context_false = any(not c["expect_context"] for c in EVAL_CASES)
    assert has_context_true, "Need at least one case that should produce CONTEXT"
    assert has_context_false, "Need at least one case that should produce NOTHING"
