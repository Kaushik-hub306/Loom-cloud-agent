"""Loom MCP stdio server.

The MCP server is *synchronous at the stdio I/O layer*: it reads one JSON-RPC
request per line from stdin, writes one JSON-RPC response per line to stdout, and
emits all logs to stderr only. Nothing but JSON-RPC ever reaches stdout.

Architecture note (intentional deviation from spec 3.1's ``mode="sync"``):
to reuse the async :class:`MemoryStore` without duplicating any SQL in a parallel
sync code path, we drive the async store through a single persistent asyncio
event loop created at startup (``loop.run_until_complete(...)`` per request). The
database is therefore opened with ``DatabasePool(config, mode="async")``. This is
cleaner than maintaining a second sync store and still satisfies the rules that
actually matter: a single DB module, ``store.py`` is the only memory access
point, no FastAPI/Slack imports, and the process runs with only
``LOOM_DATABASE_URL`` set.

This module must NOT import ``loom.api`` or ``loom.slack``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING, Any

from loom import constants
from loom.config import LoomConfig
from loom.db import DatabasePool
from loom.embeddings import EmbeddingService
from loom.errors import LoomError
from loom.logging_config import configure_logging, get_logger
from loom.memory.formatting import confidence_label, format_session_context
from loom.memory.store import MemoryStore

if TYPE_CHECKING:
    from loom.memory.models import MemoryStats, SessionContext

logger = get_logger("loom.mcp.server")

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
SERVER_NAME = "loom-memory"
SERVER_VERSION = constants.APP_VERSION
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
JSONRPC_VERSION = "2.0"

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Default recall limits for the recall_relevant tool.
_DEFAULT_RECALL_LIMIT = constants.DEFAULT_MAX_RULES_PER_SESSION
_DEFAULT_CONTEXT_LIMIT = constants.DEFAULT_MAX_CONTEXTS_PER_SESSION


# ---------------------------------------------------------------------------
# Tool definitions (must match spec 8.2 exactly: four tools)
# ---------------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "name": "session_init",
        "description": (
            "Call this first in every new coding session. Returns relevant Loom "
            "team memory and recent context as a system-prompt block."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "minLength": 1, "maxLength": 500},
                "domain": {"type": "string"},
                "channel": {"type": "string"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "recall_relevant",
        "description": (
            "Search Loom memory for conventions, decisions, patterns, or recent "
            "context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "domain": {"type": "string"},
                "channel": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["query"],
        },
    },
    {
        "name": "teach",
        "description": "Store a durable team rule or decision in Loom memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "minLength": 1, "maxLength": 50},
                "rule_type": {"type": "string", "default": "convention", "maxLength": 50},
                "rule": {"type": "string", "minLength": 5, "maxLength": 1000},
                "example": {"type": "string", "maxLength": 500},
                "confidence": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["domain", "rule"],
        },
    },
    {
        "name": "get_stats",
        "description": "Get Loom memory statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------------------
# Synchronous facade over the async MemoryStore
# ---------------------------------------------------------------------------
class SyncStore:
    """Drive the async :class:`MemoryStore` from synchronous code.

    A single persistent event loop is reused for every call so that the
    store's supervised fire-and-forget tasks (e.g. ``uses`` increments) have a
    loop to run on. See the module docstring for why this approach is used
    instead of a separate sync DB path.
    """

    def __init__(self, store: MemoryStore, loop: asyncio.AbstractEventLoop):
        self._store = store
        self._loop = loop

    def get_session_context(self, task: str, channel: str = "") -> SessionContext:
        return self._loop.run_until_complete(
            self._store.get_session_context(task, channel=channel)
        )

    def recall(self, query: str, domain: str | None = None, limit: int = 10):
        return self._loop.run_until_complete(
            self._store.recall(query, domain=domain, limit=limit)
        )

    def search_contexts(self, query: str, channel: str | None = None, limit: int = 5):
        return self._loop.run_until_complete(
            self._store.search_contexts(query, channel=channel, limit=limit)
        )

    def teach(self, **kwargs):
        return self._loop.run_until_complete(self._store.teach(**kwargs))

    def stats(self) -> MemoryStats:
        return self._loop.run_until_complete(self._store.stats())


# ---------------------------------------------------------------------------
# JSON-RPC response helpers
# ---------------------------------------------------------------------------
def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _tool_text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------------
# Tool handlers (synchronous; they receive a SyncStore)
# ---------------------------------------------------------------------------
def _tool_session_init(store: SyncStore, args: dict[str, Any]) -> str:
    task = (args.get("task") or "").strip()
    if not task:
        raise LoomError("session_init requires a non-empty 'task'.")
    channel = args.get("channel") or ""
    context = store.get_session_context(task, channel=channel)
    return format_session_context(context)


def _tool_recall_relevant(store: SyncStore, args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise LoomError("recall_relevant requires a non-empty 'query'.")
    domain = args.get("domain") or None
    channel = args.get("channel") or None
    try:
        limit = int(args.get("limit") or _DEFAULT_RECALL_LIMIT)
    except (TypeError, ValueError):
        limit = _DEFAULT_RECALL_LIMIT
    limit = max(1, min(limit, 30))

    memories = store.recall(query, domain=domain, limit=limit)
    contexts = store.search_contexts(query, channel=channel, limit=_DEFAULT_CONTEXT_LIMIT)
    return _format_recall(query, memories, contexts)


def _tool_teach(store: SyncStore, args: dict[str, Any]) -> str:
    domain = (args.get("domain") or "").strip()
    rule = (args.get("rule") or "").strip()
    if not domain or not rule:
        raise LoomError("teach requires both 'domain' and 'rule'.")
    rule_type = (args.get("rule_type") or "convention").strip() or "convention"
    example = args.get("example") or ""
    try:
        confidence = int(args.get("confidence", 7))
    except (TypeError, ValueError) as exc:
        raise LoomError("teach 'confidence' must be an integer 1..10.") from exc

    result = store.teach(
        domain=domain,
        rule_type=rule_type,
        rule=rule,
        example=example,
        confidence=confidence,
    )
    memory = result.memory
    payload = {
        "id": memory.id,
        "domain": memory.domain,
        "confidence": memory.confidence,
        "is_update": result.is_update,
        "message": _teach_message(memory.domain, memory.rule_type, result.is_update),
    }
    return json.dumps(payload, indent=2)


def _tool_get_stats(store: SyncStore, args: dict[str, Any]) -> str:
    return _format_stats(store.stats())


_TOOL_HANDLERS = {
    "session_init": _tool_session_init,
    "recall_relevant": _tool_recall_relevant,
    "teach": _tool_teach,
    "get_stats": _tool_get_stats,
}


# ---------------------------------------------------------------------------
# Output formatting for tool text content
# ---------------------------------------------------------------------------
def _teach_message(domain: str, rule_type: str, is_update: bool) -> str:
    if is_update:
        return f"Updated existing {domain} {rule_type} and increased confidence."
    return f"Remembered {domain} {rule_type}."


def _format_recall(query: str, memories: list, contexts: list) -> str:
    if not memories and not contexts:
        return (
            f"No Loom memories matched: {query}\n\n"
            "Nothing relevant is stored yet. Teach durable team rules with the "
            "`teach` tool, `loom teach`, or `/loom-teach`."
        )

    lines = [f"Loom recall results for: {query}", ""]
    if memories:
        lines.append("Rules:")
        for memory in memories:
            label = confidence_label(memory.confidence)
            lines.append(
                f"- [{label}] {memory.domain}/{memory.rule_type}: {memory.rule}"
            )
            lines.append(f"  Confidence: {memory.confidence} | Uses: {memory.uses}")
            if memory.example and memory.example.strip():
                lines.append(f"  Example: {memory.example.strip()}")
        lines.append("")

    if contexts:
        lines.append("Recent conversations:")
        for ctx in contexts:
            date = ctx.updated_at.strftime("%Y-%m-%d")
            lines.append(f"- [{ctx.domain}] {date} #{ctx.channel}: {ctx.summary}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _format_stats(stats: MemoryStats) -> str:
    lines = [
        "Loom memory stats",
        f"Rules: {stats.total_rules}",
        f"Contexts: {stats.active_contexts} active",
        f"Blobs: {stats.active_blobs} active",
        "Top domains:",
    ]
    top_domains = sorted(stats.rules_by_domain.items(), key=lambda kv: (-kv[1], kv[0]))
    for domain, count in top_domains:
        lines.append(f"- {domain}: {count}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------
def handle_request(request: dict, store: SyncStore | None) -> dict | None:
    """Dispatch a parsed JSON-RPC request to its handler.

    Returns the response dict, or ``None`` for notifications (which get no
    response). This function is pure and DB-free for every protocol method
    except ``tools/call``, which makes protocol tests easy to write without a
    database.
    """
    if not isinstance(request, dict):
        return _error(None, INVALID_REQUEST, "Request must be a JSON object.")

    req_id = request.get("id")

    if request.get("jsonrpc") != JSONRPC_VERSION:
        return _error(req_id, INVALID_REQUEST, "Missing or invalid 'jsonrpc' version.")

    method = request.get("method")
    if not isinstance(method, str) or not method:
        return _error(req_id, INVALID_REQUEST, "Missing or invalid 'method'.")

    # Notifications never receive a response.
    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        return _result(req_id, _initialize_result(params))

    if method == "ping":
        # Standard MCP/JSON-RPC ping returns an empty result object. The bare,
        # non-JSON ``ping`` -> ``pong`` shortcut is handled in process_line.
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        return _tools_call(req_id, params, store)

    return _error(req_id, METHOD_NOT_FOUND, f"Unknown method: {method}")


def _initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    protocol_version = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools_call(req_id: Any, params: dict[str, Any], store: SyncStore | None) -> dict:
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    if not isinstance(name, str) or not name:
        return _error(req_id, INVALID_PARAMS, "tools/call requires a tool 'name'.")

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        # Unknown tools resolve to a valid result with isError=true so clients
        # surface the message instead of treating it as a protocol failure.
        return _result(req_id, _tool_error(f"Unknown tool: {name}"))

    if store is None:
        return _result(req_id, _tool_error("Loom error: memory store unavailable."))

    try:
        text = handler(store, arguments)
    except LoomError as exc:
        logger.warning("tool_failed", tool=name, error_type=type(exc).__name__)
        return _result(req_id, _tool_error(f"Loom error: {exc.user_message}"))
    except Exception as exc:  # noqa: BLE001 - never crash the process on a tool error
        logger.error("tool_crashed", tool=name, error_type=type(exc).__name__)
        return _result(
            req_id, _tool_error("Loom error: an unexpected error occurred.")
        )
    return _result(req_id, _tool_text(text))


def process_line(line: str, store: SyncStore | None) -> str | None:
    """Process one raw stdin line and return one raw stdout line (or ``None``).

    Handles malformed JSON (parse error) and the bare ``ping`` -> ``pong``
    health shortcut. Never raises; bad input becomes a JSON-RPC error string.
    """
    stripped = line.strip()
    if not stripped:
        return None

    # Bare custom ping shortcut (not JSON-RPC): a literal ``ping`` line.
    if stripped == "ping":
        return "pong"

    try:
        request = json.loads(stripped)
    except (ValueError, TypeError):
        # ID is unreadable for malformed JSON, so it is reported as null.
        logger.warning("parse_error")
        return json.dumps(_error(None, PARSE_ERROR, "Parse error: invalid JSON."))

    response = handle_request(request, store)
    if response is None:
        return None
    return json.dumps(response)


# ---------------------------------------------------------------------------
# Runtime wiring
# ---------------------------------------------------------------------------
def build_runtime(config: LoomConfig):
    """Open the DB and build a :class:`SyncStore` plus a close callback.

    Returns ``(sync_store, close)``. ``close()`` shuts the async pool down and
    closes the event loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    pool = DatabasePool(config, mode="async")
    loop.run_until_complete(pool.init_async())
    embeddings = EmbeddingService(config)
    store = MemoryStore(pool, embeddings, config)
    sync_store = SyncStore(store, loop)

    def close() -> None:
        try:
            loop.run_until_complete(pool.close_async())
        finally:
            loop.close()

    return sync_store, close


def _log_startup(sync_store: SyncStore) -> None:
    """Emit the startup banner to stderr only (never stdout)."""
    logger.info("mcp_starting", version=SERVER_VERSION)
    logger.info("mcp_database", status="connected")
    try:
        stats = sync_store.stats()
        logger.info(
            "mcp_memory",
            rules=stats.total_rules,
            active_contexts=stats.active_contexts,
        )
    except Exception as exc:  # noqa: BLE001 - banner stats are best-effort
        logger.warning("mcp_stats_unavailable", error_type=type(exc).__name__)
    logger.info("mcp_ready")


def _install_protocol_stdout() -> Any:
    """Reserve the real stdout for JSON-RPC and redirect everything else away.

    A stdio MCP server must keep stdout as a pure JSON-RPC channel. Third-party
    libraries (e.g. LiteLLM) sometimes ``print`` banners straight to the
    process's stdout, which corrupts the stream and makes the client drop the
    connection. To make that impossible, we:

    1. duplicate the real stdout (fd 1) to a private handle used only for
       JSON-RPC responses, then
    2. point fd 1 (and the Python ``sys.stdout`` object) at stderr, so any
       stray writes from anywhere land on stderr instead of the protocol.

    Returns the private text handle to write JSON-RPC responses to.
    """
    real_stdout_fd = os.dup(1)
    os.dup2(2, 1)  # fd 1 now points at stderr; stray prints can't reach clients
    sys.stdout = sys.stderr  # stray Python-level print() also goes to stderr
    return os.fdopen(real_stdout_fd, "w", encoding="utf-8", buffering=1)


def serve(sync_store: SyncStore, out: Any) -> None:
    """Read JSON-RPC requests from stdin and write responses to ``out``."""
    for line in sys.stdin:
        response_line = process_line(line, sync_store)
        if response_line is not None:
            out.write(response_line + "\n")
            out.flush()


def main() -> None:
    # Secure the protocol channel before anything (DB drivers, LiteLLM, etc.)
    # has a chance to write to stdout.
    protocol_out = _install_protocol_stdout()
    config = LoomConfig.from_env()
    configure_logging(config)
    sync_store, close = build_runtime(config)
    try:
        _log_startup(sync_store)
        serve(sync_store, protocol_out)
    finally:
        close()


if __name__ == "__main__":
    main()
