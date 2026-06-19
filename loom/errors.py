"""Typed errors for Loom.

All Loom errors inherit from :class:`LoomError`. Each error carries a
user-safe ``user_message`` and an optional non-secret ``details`` dict. API
handlers convert these into JSON error responses using ``code`` and
``user_message``.
"""

from __future__ import annotations

from typing import Any


class LoomError(Exception):
    """Base class for all Loom errors."""

    code: str = "loom_error"
    user_message: str = "An unexpected Loom error occurred."

    def __init__(
        self,
        user_message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        # ``details`` must already be redacted by the caller; it may be logged.
        self.details: dict[str, Any] = details or {}
        if user_message is not None:
            self.user_message = user_message
        super().__init__(self.user_message)

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.user_message}


class ConfigError(LoomError):
    code = "config_error"
    user_message = "Loom configuration is invalid."


class LoomDBError(LoomError):
    code = "database_error"
    user_message = "A database error occurred."


class EmbeddingError(LoomError):
    code = "embedding_error"
    user_message = "An embedding error occurred."


class LLMError(LoomError):
    code = "llm_error"
    user_message = "An LLM error occurred."


class APIAuthError(LoomError):
    code = "api_auth_error"
    user_message = "Authentication failed."


class SlackConfigError(LoomError):
    code = "slack_config_error"
    user_message = "Slack configuration is invalid."


class SlackAPIError(LoomError):
    code = "slack_api_error"
    user_message = "A Slack API error occurred."


class MCPProtocolError(LoomError):
    code = "mcp_protocol_error"
    user_message = "An MCP protocol error occurred."


class ImportExportError(LoomError):
    code = "import_export_error"
    user_message = "An import/export error occurred."
