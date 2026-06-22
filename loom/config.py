"""Loom configuration.

This is the ONLY module permitted to read environment variables. Everything
else receives a fully-built, validated :class:`LoomConfig` instance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal

import structlog

from loom.errors import ConfigError
from loom.utils import redact_secret

logger = structlog.get_logger("loom.config")

EnvironmentName = Literal["development", "test", "production"]
LLMProvider = Literal["deepseek", "gemini", "claude", "openai", "skip"]
EmbeddingProvider = Literal["gemini", "none"]
DBMode = Literal["async", "sync"]

_VALID_ENVS = ("development", "test", "production")
_VALID_LLM_PROVIDERS = ("deepseek", "gemini", "claude", "openai", "skip")
_VALID_EMBEDDING_PROVIDERS = ("gemini", "none")

PROVIDER_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

EMBEDDING_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "none": None,
}

DEFAULT_LLM_MODELS = {
    "deepseek": "deepseek/deepseek-chat",
    "gemini": "gemini/gemini-1.5-flash",
    "claude": "anthropic/claude-3-5-haiku-latest",
    "openai": "openai/gpt-4o-mini",
    "skip": None,
}

_TRUE_VALUES = {"true", "1", "yes", "y", "on"}
_FALSE_VALUES = {"false", "0", "no", "n", "off"}

_dotenv_loaded = False


def _load_dotenv_file() -> None:
    """Load a local ``.env`` file into ``os.environ`` exactly once.

    This is the single place in Loom that bridges a ``.env`` file into the
    process environment, keeping ``config.py`` the only module that reads
    environment variables. Real environment variables always win over the
    ``.env`` file (``override=False``), so explicit shell exports and
    container/Railway settings are never clobbered.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    # No logging here: this runs during config load, before logging is
    # configured, and the MCP server requires stdout to carry JSON-RPC only.
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)


@dataclass(frozen=True)
class CredentialCheck:
    name: str
    status: Literal["ok", "fail", "skipped"]
    message: str


@dataclass(frozen=True)
class CredentialStatus:
    database: CredentialCheck
    slack_bot: CredentialCheck
    slack_app: CredentialCheck
    llm: CredentialCheck
    embedding: CredentialCheck

    @property
    def ok(self) -> bool:
        return all(
            check.status != "fail"
            for check in [
                self.database,
                self.slack_bot,
                self.slack_app,
                self.llm,
                self.embedding,
            ]
        )

    def as_list(self) -> list[CredentialCheck]:
        return [self.database, self.slack_bot, self.slack_app, self.llm, self.embedding]


@dataclass(frozen=True)
class LoomConfig:
    database_url: str

    env: EnvironmentName = "development"

    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    slack_silent: bool = True

    api_key: str | None = None
    internal_api_token: str | None = None
    api_base_url: str = "http://localhost:8000"

    llm_provider: LLMProvider = "deepseek"
    llm_api_key: str | None = None
    llm_model_override: str | None = None

    embedding_provider: EmbeddingProvider = "gemini"
    embedding_api_key: str | None = None
    embedding_model: str = "gemini/text-embedding-004"
    embedding_dimension: int = 768
    embedding_input_max_chars: int = 3000
    embedding_cache_size: int = 256

    max_rules_per_session: int = 10
    max_contexts_per_session: int = 3
    context_max_chars: int = 500
    context_ttl_days: int = 30
    blob_ttl_days: int = 14
    context_half_life_days: float = 10.0

    gatekeeper_idle_seconds: int = 180
    gatekeeper_debounce_seconds: int = 180
    slack_buffer_max_messages: int = 30
    slack_reconnect_backoff_max_seconds: int = 60

    api_port: int = 8000
    api_host: str = "0.0.0.0"  # noqa: S104 - intended default for containers
    log_level: str = "INFO"
    http_timeout_seconds: int = 30
    llm_timeout_seconds: int = 45

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def llm_model_id(self) -> str | None:
        if self.llm_provider == "skip":
            return None
        if self.llm_model_override:
            return self.llm_model_override
        return DEFAULT_LLM_MODELS.get(self.llm_provider)

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "skip" and bool(self.llm_api_key)

    @property
    def embedding_enabled(self) -> bool:
        return self.embedding_provider != "none" and bool(self.embedding_api_key)

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_bot_token) and bool(self.slack_app_token)

    def with_overrides(self, **kwargs) -> LoomConfig:
        """Return a copy with overridden fields (used by CLI flags/tests)."""
        return replace(self, **kwargs)

    # ------------------------------------------------------------------
    # Environment loading
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> LoomConfig:
        if environ is None:
            _load_dotenv_file()
            env = dict(os.environ)
        else:
            env = environ
        errors: list[str] = []

        def get_str(key: str, default: str | None = None) -> str | None:
            raw = env.get(key)
            if raw is None:
                return default
            raw = raw.strip()
            return raw if raw != "" else default

        def get_int(key: str, default: int) -> int:
            raw = env.get(key)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw.strip())
            except (TypeError, ValueError):
                errors.append(f"{key} must be an integer (got {raw!r})")
                return default

        def get_float(key: str, default: float) -> float:
            raw = env.get(key)
            if raw is None or raw.strip() == "":
                return default
            try:
                return float(raw.strip())
            except (TypeError, ValueError):
                errors.append(f"{key} must be a float (got {raw!r})")
                return default

        def get_bool(key: str, default: bool) -> bool:
            raw = env.get(key)
            if raw is None or raw.strip() == "":
                return default
            normalized = raw.strip().lower()
            if normalized in _TRUE_VALUES:
                return True
            if normalized in _FALSE_VALUES:
                return False
            errors.append(f"{key} must be a boolean (got {raw!r})")
            return default

        database_url = get_str("LOOM_DATABASE_URL")
        if not database_url:
            errors.append("LOOM_DATABASE_URL is required")

        env_name = get_str("LOOM_ENV", "development")
        if env_name not in _VALID_ENVS:
            errors.append(
                f"LOOM_ENV must be one of {_VALID_ENVS} (got {env_name!r})"
            )
            env_name = "development"

        llm_provider = get_str("LOOM_LLM_PROVIDER", "deepseek")
        if llm_provider not in _VALID_LLM_PROVIDERS:
            errors.append(
                f"LOOM_LLM_PROVIDER must be one of {_VALID_LLM_PROVIDERS} "
                f"(got {llm_provider!r})"
            )
            llm_provider = "deepseek"

        embedding_provider = get_str("LOOM_EMBEDDING_PROVIDER", "gemini")
        if embedding_provider not in _VALID_EMBEDDING_PROVIDERS:
            errors.append(
                f"LOOM_EMBEDDING_PROVIDER must be one of "
                f"{_VALID_EMBEDDING_PROVIDERS} (got {embedding_provider!r})"
            )
            embedding_provider = "gemini"

        slack_bot_token = get_str("LOOM_SLACK_BOT_TOKEN")
        slack_app_token = get_str("LOOM_SLACK_APP_TOKEN")
        slack_silent = get_bool("LOOM_SLACK_SILENT", True)

        api_key = get_str("LOOM_API_KEY")
        internal_api_token = get_str("LOOM_INTERNAL_API_TOKEN")
        api_base_url = get_str("LOOM_API_BASE_URL", "http://localhost:8000")

        llm_model_override = get_str("LOOM_LLM_MODEL_OVERRIDE")
        llm_api_key: str | None = None
        if llm_provider != "skip":
            llm_key_env = PROVIDER_KEY_ENV.get(llm_provider)
            if llm_key_env:
                llm_api_key = get_str(llm_key_env)

        embedding_key_env = EMBEDDING_KEY_ENV.get(embedding_provider)
        embedding_api_key = get_str(embedding_key_env) if embedding_key_env else None
        embedding_model = get_str("LOOM_EMBEDDING_MODEL", "gemini/text-embedding-004")
        embedding_dimension = get_int("LOOM_EMBEDDING_DIMENSION", 768)
        embedding_input_max_chars = get_int("LOOM_EMBEDDING_INPUT_MAX_CHARS", 3000)
        embedding_cache_size = get_int("LOOM_EMBEDDING_CACHE_SIZE", 256)

        max_rules_per_session = get_int("LOOM_MAX_RULES_PER_SESSION", 10)
        max_contexts_per_session = get_int("LOOM_MAX_CONTEXTS_PER_SESSION", 3)
        context_max_chars = get_int("LOOM_CONTEXT_MAX_CHARS", 500)
        context_ttl_days = get_int("LOOM_CONTEXT_TTL_DAYS", 30)
        blob_ttl_days = get_int("LOOM_BLOB_TTL_DAYS", 14)
        context_half_life_days = get_float("LOOM_CONTEXT_HALF_LIFE_DAYS", 10.0)

        gatekeeper_idle_seconds = get_int("LOOM_GATEKEEPER_IDLE_SECONDS", 180)
        gatekeeper_debounce_seconds = get_int("LOOM_GATEKEEPER_DEBOUNCE_SECONDS", 180)
        slack_buffer_max_messages = get_int("LOOM_SLACK_BUFFER_MAX_MESSAGES", 30)
        slack_reconnect_backoff_max_seconds = get_int(
            "LOOM_SLACK_RECONNECT_BACKOFF_MAX_SECONDS", 60
        )

        api_port = get_int("LOOM_API_PORT", 8000)
        api_host = get_str("LOOM_API_HOST", "0.0.0.0")  # noqa: S104
        log_level = get_str("LOOM_LOG_LEVEL", "INFO")
        http_timeout_seconds = get_int("LOOM_HTTP_TIMEOUT_SECONDS", 30)
        llm_timeout_seconds = get_int("LOOM_LLM_TIMEOUT_SECONDS", 45)

        # Cross-field rules.
        if bool(slack_bot_token) != bool(slack_app_token):
            errors.append(
                "Slack requires both LOOM_SLACK_BOT_TOKEN and "
                "LOOM_SLACK_APP_TOKEN, or neither"
            )

        if env_name == "production":
            if not api_key:
                errors.append("LOOM_API_KEY is required in production")
            if not internal_api_token:
                errors.append("LOOM_INTERNAL_API_TOKEN is required in production")

        # Range checks.
        positive_int_fields = {
            "LOOM_EMBEDDING_DIMENSION": embedding_dimension,
            "LOOM_EMBEDDING_INPUT_MAX_CHARS": embedding_input_max_chars,
            "LOOM_MAX_RULES_PER_SESSION": max_rules_per_session,
            "LOOM_MAX_CONTEXTS_PER_SESSION": max_contexts_per_session,
            "LOOM_CONTEXT_MAX_CHARS": context_max_chars,
            "LOOM_CONTEXT_TTL_DAYS": context_ttl_days,
            "LOOM_BLOB_TTL_DAYS": blob_ttl_days,
            "LOOM_GATEKEEPER_IDLE_SECONDS": gatekeeper_idle_seconds,
            "LOOM_GATEKEEPER_DEBOUNCE_SECONDS": gatekeeper_debounce_seconds,
            "LOOM_SLACK_BUFFER_MAX_MESSAGES": slack_buffer_max_messages,
            "LOOM_SLACK_RECONNECT_BACKOFF_MAX_SECONDS": slack_reconnect_backoff_max_seconds,
            "LOOM_HTTP_TIMEOUT_SECONDS": http_timeout_seconds,
            "LOOM_LLM_TIMEOUT_SECONDS": llm_timeout_seconds,
        }
        for key, val in positive_int_fields.items():
            if val <= 0:
                errors.append(f"{key} must be a positive integer (got {val})")

        if embedding_cache_size < 0:
            errors.append(
                f"LOOM_EMBEDDING_CACHE_SIZE must be >= 0 (got {embedding_cache_size})"
            )

        if context_half_life_days <= 0:
            errors.append(
                "LOOM_CONTEXT_HALF_LIFE_DAYS must be positive "
                f"(got {context_half_life_days})"
            )

        if not (1 <= api_port <= 65535):
            errors.append(f"LOOM_API_PORT must be in 1..65535 (got {api_port})")

        if errors:
            raise ConfigError(
                "Invalid Loom configuration: " + "; ".join(errors),
                details={"invalid_fields": errors},
            )

        return cls(
            database_url=database_url,  # type: ignore[arg-type]
            env=env_name,  # type: ignore[arg-type]
            slack_bot_token=slack_bot_token,
            slack_app_token=slack_app_token,
            slack_silent=slack_silent,
            api_key=api_key,
            internal_api_token=internal_api_token,
            api_base_url=api_base_url,  # type: ignore[arg-type]
            llm_provider=llm_provider,  # type: ignore[arg-type]
            llm_api_key=llm_api_key,
            llm_model_override=llm_model_override,
            embedding_provider=embedding_provider,  # type: ignore[arg-type]
            embedding_api_key=embedding_api_key,
            embedding_model=embedding_model,  # type: ignore[arg-type]
            embedding_dimension=embedding_dimension,
            embedding_input_max_chars=embedding_input_max_chars,
            embedding_cache_size=embedding_cache_size,
            max_rules_per_session=max_rules_per_session,
            max_contexts_per_session=max_contexts_per_session,
            context_max_chars=context_max_chars,
            context_ttl_days=context_ttl_days,
            blob_ttl_days=blob_ttl_days,
            context_half_life_days=context_half_life_days,
            gatekeeper_idle_seconds=gatekeeper_idle_seconds,
            gatekeeper_debounce_seconds=gatekeeper_debounce_seconds,
            slack_buffer_max_messages=slack_buffer_max_messages,
            slack_reconnect_backoff_max_seconds=slack_reconnect_backoff_max_seconds,
            api_port=api_port,
            api_host=api_host,  # type: ignore[arg-type]
            log_level=log_level,  # type: ignore[arg-type]
            http_timeout_seconds=http_timeout_seconds,
            llm_timeout_seconds=llm_timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Credential validation
    # ------------------------------------------------------------------
    async def validate_credentials(self, strict: bool = False) -> CredentialStatus:
        """Probe configured services. ``strict`` is used by ``loom init``."""
        database = await self._check_database()
        slack_bot, slack_app = await self._check_slack()
        llm = await self._check_llm(strict=strict)
        embedding = await self._check_embedding()
        return CredentialStatus(
            database=database,
            slack_bot=slack_bot,
            slack_app=slack_app,
            llm=llm,
            embedding=embedding,
        )

    async def _check_database(self) -> CredentialCheck:
        from loom.db import probe_database_async

        try:
            latency_ms = await probe_database_async(self.database_url)
            return CredentialCheck(
                "database", "ok", f"PostgreSQL reachable ({latency_ms:.0f}ms)"
            )
        except Exception as exc:  # noqa: BLE001 - report as typed check result
            logger.warning("database_check_failed", error_type=type(exc).__name__)
            return CredentialCheck("database", "fail", f"Cannot connect: {exc}")

    async def _check_slack(self) -> tuple[CredentialCheck, CredentialCheck]:
        if not self.slack_bot_token and not self.slack_app_token:
            skipped = CredentialCheck("slack_bot", "skipped", "Slack not configured")
            return skipped, CredentialCheck(
                "slack_app", "skipped", "Slack not configured"
            )

        bot_check = CredentialCheck("slack_bot", "fail", "Bot token invalid")
        app_check = CredentialCheck("slack_app", "fail", "App token invalid")

        try:
            from slack_sdk.web.async_client import AsyncWebClient

            client = AsyncWebClient(token=self.slack_bot_token)
            resp = await client.auth_test()
            team = resp.get("team", "unknown")
            bot_check = CredentialCheck("slack_bot", "ok", f"workspace: {team}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack_bot_check_failed", error_type=type(exc).__name__)
            bot_check = CredentialCheck("slack_bot", "fail", f"Bot token invalid: {exc}")

        try:
            from slack_sdk.web.async_client import AsyncWebClient

            app_client = AsyncWebClient(token=self.slack_app_token)
            await app_client.apps_connections_open(app_token=self.slack_app_token)
            app_check = CredentialCheck("slack_app", "ok", "Socket Mode reachable")
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack_app_check_failed", error_type=type(exc).__name__)
            app_check = CredentialCheck("slack_app", "fail", f"App token invalid: {exc}")

        return bot_check, app_check

    async def _check_llm(self, *, strict: bool) -> CredentialCheck:
        if self.llm_provider == "skip":
            return CredentialCheck("llm", "skipped", "LLM disabled (provider=skip)")
        if not self.llm_api_key:
            status = "fail" if strict else "skipped"
            return CredentialCheck("llm", status, "LLM API key missing")

        try:
            import litellm

            await litellm.acompletion(
                model=self.llm_model_id,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                api_key=self.llm_api_key,
            )
            return CredentialCheck("llm", "ok", f"model: {self.llm_model_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_check_failed", error_type=type(exc).__name__)
            return CredentialCheck("llm", "fail", f"LLM key invalid: {exc}")

    async def _check_embedding(self) -> CredentialCheck:
        if self.embedding_provider == "none":
            return CredentialCheck("embedding", "skipped", "Embeddings disabled")
        if not self.embedding_api_key:
            return CredentialCheck(
                "embedding", "skipped", "No embedding key; using text search"
            )

        try:
            import litellm

            resp = await litellm.aembedding(
                model=self.embedding_model,
                input=["ping"],
                api_key=self.embedding_api_key,
            )
            vector = resp["data"][0]["embedding"]
            if len(vector) != self.embedding_dimension:
                return CredentialCheck(
                    "embedding",
                    "fail",
                    f"dimension mismatch: got {len(vector)}, "
                    f"expected {self.embedding_dimension}",
                )
            return CredentialCheck("embedding", "ok", f"model: {self.embedding_model}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding_check_failed", error_type=type(exc).__name__)
            return CredentialCheck("embedding", "fail", f"Embedding key invalid: {exc}")

    def safe_summary(self) -> dict[str, str]:
        """A log-safe summary of the config with secrets redacted."""
        return {
            "env": self.env,
            "llm_provider": self.llm_provider,
            "llm_api_key": redact_secret(self.llm_api_key),
            "embedding_provider": self.embedding_provider,
            "embedding_api_key": redact_secret(self.embedding_api_key),
            "api_key": redact_secret(self.api_key),
            "internal_api_token": redact_secret(self.internal_api_token),
            "slack_bot_token": redact_secret(self.slack_bot_token),
            "slack_app_token": redact_secret(self.slack_app_token),
            "database_url": redact_secret(self.database_url, visible_prefix=12),
        }
