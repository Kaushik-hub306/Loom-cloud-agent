"""Phase 3 MCP stdio server tests.

Protocol tests (initialize, tools/list, ping, malformed JSON, unknown method,
no-fastapi/slack import) are DB-free and always run. Tool-execution tests build
a real ``MemoryStore`` against ``TEST_DATABASE_URL`` and are skipped when it is
unset (mirrors ``tests/conftest.py::requires_db``).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from loom.config import LoomConfig
from loom.db import DatabasePool
from loom.mcp import server
from loom.memory.store import MemoryStore
from tests.conftest import TEST_DATABASE_URL, requires_db


class ConstEmbeddings:
    """Fake embedding service returning a constant non-null vector.

    Defined locally (per Phase 3 instructions) so protocol/tool tests do not
    depend on a real embedding provider or on conftest.
    """

    def __init__(self, dimension: int = 768):
        self.dimension = dimension

    async def embed(self, text: str):
        if not text or not text.strip():
            return None
        return [0.1] * self.dimension


@pytest.fixture
def mcp_store():
    """A SyncStore backed by a real MemoryStore against the test DB."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")

    config = LoomConfig(
        database_url=TEST_DATABASE_URL,
        env="test",
        llm_provider="skip",
        llm_api_key=None,
        embedding_provider="none",
        embedding_api_key=None,
        api_key=None,
        internal_api_token=None,
    )
    loop = asyncio.new_event_loop()
    pool = DatabasePool(config, mode="async")
    loop.run_until_complete(pool.init_async())
    loop.run_until_complete(
        pool.execute("TRUNCATE memories, conversation_contexts, conversation_blobs")
    )
    store = MemoryStore(pool, ConstEmbeddings(config.embedding_dimension), config)
    sync_store = server.SyncStore(store, loop)
    try:
        yield sync_store
    finally:
        loop.run_until_complete(pool.close_async())
        loop.close()


def _call_tool(store, name: str, arguments: dict, req_id: int = 99) -> dict:
    request = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return server.handle_request(request, store)


# ---------------------------------------------------------------------------
# Protocol tests (DB-free)
# ---------------------------------------------------------------------------
def test_initialize_returns_correct_protocol_version():
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "client", "version": "0.1.0"},
        },
    }
    resp = server.handle_request(request, None)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2025-03-26"
    assert resp["result"]["capabilities"] == {"tools": {}}
    assert resp["result"]["serverInfo"] == {"name": "loom-memory", "version": "1.0.0"}

    # Defaults to 2024-11-05 when the client omits protocolVersion.
    resp_default = server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}, None
    )
    assert resp_default["result"]["protocolVersion"] == "2024-11-05"


def test_notifications_initialized_returns_no_response():
    request = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert server.handle_request(request, None) is None


def test_tools_list_returns_all_four_tools():
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, None
    )
    tools = resp["result"]["tools"]
    assert len(tools) == 4
    assert {t["name"] for t in tools} == {
        "session_init",
        "recall_relevant",
        "teach",
        "get_stats",
    }


def test_tools_have_valid_input_schemas():
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, None
    )
    by_name = {t["name"]: t for t in resp["result"]["tools"]}
    for tool in by_name.values():
        assert tool["name"]
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema

    assert by_name["session_init"]["inputSchema"]["required"] == ["task"]
    assert by_name["recall_relevant"]["inputSchema"]["required"] == ["query"]
    assert by_name["teach"]["inputSchema"]["required"] == ["domain", "rule"]
    assert by_name["get_stats"]["inputSchema"]["properties"] == {}


def test_ping_returns_pong():
    # Bare, non-JSON ping shortcut.
    assert server.process_line("ping", None) == "pong"
    # Standard JSON-RPC ping returns an empty result object.
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "ping"}, None
    )
    assert resp["result"] == {}


def test_unknown_tool_returns_error_without_crash():
    resp = _call_tool(None, "does_not_exist", {})
    assert "error" not in resp
    result = resp["result"]
    assert result["isError"] is True
    assert "Unknown tool" in result["content"][0]["text"]


def test_unknown_method_returns_json_rpc_error():
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "no/such/method"}, None
    )
    assert "result" not in resp
    assert resp["error"]["code"] == server.METHOD_NOT_FOUND


def test_server_survives_malformed_json_input():
    # Malformed JSON -> parse error with null id, no crash.
    out = server.process_line("{not valid json", None)
    parsed = json.loads(out)
    assert parsed["error"]["code"] == server.PARSE_ERROR
    assert parsed["id"] is None

    # Missing jsonrpc version -> invalid request, no crash.
    out_invalid = server.process_line(
        json.dumps({"id": 1, "method": "initialize"}), None
    )
    assert json.loads(out_invalid)["error"]["code"] == server.INVALID_REQUEST

    # Blank line -> no response.
    assert server.process_line("   ", None) is None


def test_server_runs_without_fastapi_or_slack():
    # Importing the MCP server must not pull in FastAPI or Slack modules.
    code = (
        "import importlib, sys\n"
        "importlib.import_module('loom.mcp.server')\n"
        "forbidden = [\n"
        "    m for m in sys.modules\n"
        "    if m in ('loom.api', 'loom.slack', 'fastapi', 'slack_bolt')\n"
        "    or m.startswith('loom.api.') or m.startswith('loom.slack.')\n"
        "]\n"
        "assert not forbidden, forbidden\n"
        "print('IMPORT_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "IMPORT_OK" in proc.stdout

    # Belt-and-suspenders: the source itself must not import the forbidden modules.
    source = (
        os.path.join(os.path.dirname(__file__), "..", "loom", "mcp", "server.py")
    )
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    assert "import loom.api" not in text
    assert "import loom.slack" not in text
    assert "from loom.api" not in text
    assert "from loom.slack" not in text


# ---------------------------------------------------------------------------
# Tool-execution tests (require TEST_DATABASE_URL)
# ---------------------------------------------------------------------------
@requires_db
def test_session_init_returns_formatted_context_block(mcp_store):
    mcp_store.teach(
        domain="coding",
        rule_type="convention",
        rule="Use async/await for all I/O operations",
        example="Use asyncpg in FastAPI routes.",
        confidence=8,
    )
    resp = _call_tool(mcp_store, "session_init", {"task": "Refactor an I/O path"})
    assert resp["result"]["isError"] is False
    text = resp["result"]["content"][0]["text"]
    assert "<!-- LOOM:SESSION_CONTEXT -->" in text
    assert "<!-- /LOOM:SESSION_CONTEXT -->" in text
    assert "async/await" in text


@requires_db
def test_session_init_with_no_memories_returns_helpful_message(mcp_store):
    resp = _call_tool(mcp_store, "session_init", {"task": "Anything at all"})
    text = resp["result"]["content"][0]["text"]
    assert "0 rules | 0 past conversations" in text
    assert "Follow user instructions first." in text


@requires_db
def test_recall_relevant_returns_scored_results(mcp_store):
    mcp_store.teach(
        domain="coding",
        rule_type="convention",
        rule="Use dependency-injected middleware factories for auth",
        confidence=9,
    )
    resp = _call_tool(mcp_store, "recall_relevant", {"query": "auth middleware"})
    assert resp["result"]["isError"] is False
    text = resp["result"]["content"][0]["text"]
    assert "Loom recall results for: auth middleware" in text
    assert "Rules:" in text
    assert "[HIGH]" in text
    assert "Confidence: 9" in text


@requires_db
def test_teach_stores_and_returns_confirmation(mcp_store):
    resp = _call_tool(
        mcp_store,
        "teach",
        {
            "domain": "coding",
            "rule_type": "convention",
            "rule": "Use async/await for I/O.",
            "example": "Use asyncpg in FastAPI routes.",
            "confidence": 7,
        },
    )
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["domain"] == "coding"
    assert payload["confidence"] == 7
    assert payload["is_update"] is False
    assert payload["id"].startswith("coding::convention::")
    assert "Remembered" in payload["message"]

    # A duplicate teach bumps confidence and reports is_update.
    resp2 = _call_tool(
        mcp_store,
        "teach",
        {
            "domain": "coding",
            "rule_type": "convention",
            "rule": "Use async/await for I/O.",
            "confidence": 7,
        },
    )
    payload2 = json.loads(resp2["result"]["content"][0]["text"])
    assert payload2["is_update"] is True
    assert payload2["confidence"] == 8


@requires_db
def test_get_stats_returns_counts(mcp_store):
    mcp_store.teach(
        domain="coding", rule_type="convention",
        rule="Stats rule number one here", confidence=7,
    )
    mcp_store.teach(
        domain="architecture", rule_type="decision",
        rule="Stats rule number two here", confidence=8,
    )
    resp = _call_tool(mcp_store, "get_stats", {})
    text = resp["result"]["content"][0]["text"]
    assert "Loom memory stats" in text
    assert "Rules: 2" in text
    assert "- coding: 1" in text
    assert "- architecture: 1" in text


@requires_db
def test_server_logs_to_stderr_not_stdout():
    env = dict(os.environ)
    env["LOOM_DATABASE_URL"] = TEST_DATABASE_URL
    env["LOOM_LLM_PROVIDER"] = "skip"
    env["LOOM_EMBEDDING_PROVIDER"] = "none"
    env["LOOM_ENV"] = "development"

    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "loom.mcp.server"],
        input=request + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    # stdout must contain ONLY valid JSON-RPC (one response line, parseable).
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) == 1, proc.stdout
    parsed = json.loads(stdout_lines[0])
    assert parsed["id"] == 1
    assert parsed["result"]["serverInfo"]["name"] == "loom-memory"

    # Startup logs must appear on stderr.
    assert proc.stderr.strip() != ""
    assert "mcp_ready" in proc.stderr or "mcp_starting" in proc.stderr
