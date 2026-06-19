"""Phase 1 config tests."""

from __future__ import annotations

import pytest

from loom.config import LoomConfig
from loom.errors import ConfigError
from loom.utils import redact_secret


def _full_env() -> dict[str, str]:
    return {
        "LOOM_DATABASE_URL": "postgresql://u:p@host:5432/db",
        "LOOM_ENV": "development",
        "LOOM_SLACK_BOT_TOKEN": "xoxb-abc",
        "LOOM_SLACK_APP_TOKEN": "xapp-abc",
        "LOOM_SLACK_SILENT": "true",
        "LOOM_API_KEY": "api-secret",
        "LOOM_INTERNAL_API_TOKEN": "internal-secret",
        "LOOM_API_BASE_URL": "http://api:9000",
        "LOOM_LLM_PROVIDER": "openai",
        "LOOM_LLM_MODEL_OVERRIDE": "openai/gpt-4o",
        "OPENAI_API_KEY": "sk-openai",
        "LOOM_EMBEDDING_PROVIDER": "gemini",
        "GEMINI_API_KEY": "gem-key",
        "LOOM_EMBEDDING_MODEL": "gemini/text-embedding-004",
        "LOOM_EMBEDDING_DIMENSION": "768",
        "LOOM_EMBEDDING_INPUT_MAX_CHARS": "3000",
        "LOOM_EMBEDDING_CACHE_SIZE": "256",
        "LOOM_MAX_RULES_PER_SESSION": "12",
        "LOOM_MAX_CONTEXTS_PER_SESSION": "4",
        "LOOM_CONTEXT_MAX_CHARS": "400",
        "LOOM_CONTEXT_TTL_DAYS": "20",
        "LOOM_BLOB_TTL_DAYS": "7",
        "LOOM_CONTEXT_HALF_LIFE_DAYS": "5.5",
        "LOOM_GATEKEEPER_IDLE_SECONDS": "100",
        "LOOM_GATEKEEPER_DEBOUNCE_SECONDS": "120",
        "LOOM_SLACK_BUFFER_MAX_MESSAGES": "25",
        "LOOM_SLACK_RECONNECT_BACKOFF_MAX_SECONDS": "30",
        "LOOM_API_PORT": "8123",
        "LOOM_API_HOST": "127.0.0.1",
        "LOOM_LOG_LEVEL": "DEBUG",
        "LOOM_HTTP_TIMEOUT_SECONDS": "15",
        "LOOM_LLM_TIMEOUT_SECONDS": "40",
    }


def test_config_raises_on_missing_database_url():
    with pytest.raises(ConfigError) as exc:
        LoomConfig.from_env({})
    assert "LOOM_DATABASE_URL" in str(exc.value)


def test_config_raises_with_all_missing_fields_listed():
    with pytest.raises(ConfigError) as exc:
        LoomConfig.from_env({"LOOM_ENV": "production"})
    message = str(exc.value)
    assert "LOOM_DATABASE_URL" in message
    assert "LOOM_API_KEY" in message
    assert "LOOM_INTERNAL_API_TOKEN" in message


def test_config_from_env_parses_all_fields():
    config = LoomConfig.from_env(_full_env())
    assert config.database_url == "postgresql://u:p@host:5432/db"
    assert config.env == "development"
    assert config.slack_bot_token == "xoxb-abc"
    assert config.slack_app_token == "xapp-abc"
    assert config.slack_silent is True
    assert config.api_key == "api-secret"
    assert config.internal_api_token == "internal-secret"
    assert config.api_base_url == "http://api:9000"
    assert config.llm_provider == "openai"
    assert config.llm_api_key == "sk-openai"
    assert config.llm_model_id == "openai/gpt-4o"  # override wins
    assert config.embedding_provider == "gemini"
    assert config.embedding_api_key == "gem-key"
    assert config.embedding_dimension == 768
    assert config.max_rules_per_session == 12
    assert config.max_contexts_per_session == 4
    assert config.context_max_chars == 400
    assert config.context_ttl_days == 20
    assert config.blob_ttl_days == 7
    assert config.context_half_life_days == 5.5
    assert config.gatekeeper_idle_seconds == 100
    assert config.gatekeeper_debounce_seconds == 120
    assert config.slack_buffer_max_messages == 25
    assert config.slack_reconnect_backoff_max_seconds == 30
    assert config.api_port == 8123
    assert config.api_host == "127.0.0.1"
    assert config.log_level == "DEBUG"
    assert config.http_timeout_seconds == 15
    assert config.llm_timeout_seconds == 40


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("1", True), ("yes", True), ("y", True), ("on", True),
        ("TRUE", True), ("Yes", True),
        ("false", False), ("0", False), ("no", False), ("n", False),
        ("off", False), ("FALSE", False),
    ],
)
def test_config_parses_boolean_variants(raw, expected):
    config = LoomConfig.from_env(
        {"LOOM_DATABASE_URL": "postgresql://x", "LOOM_SLACK_SILENT": raw}
    )
    assert config.slack_silent is expected


def test_config_parses_invalid_boolean_raises():
    with pytest.raises(ConfigError) as exc:
        LoomConfig.from_env(
            {"LOOM_DATABASE_URL": "postgresql://x", "LOOM_SLACK_SILENT": "maybe"}
        )
    assert "LOOM_SLACK_SILENT" in str(exc.value)


def test_config_maps_provider_keys_correctly():
    for provider, env_key in [
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("claude", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
    ]:
        config = LoomConfig.from_env(
            {
                "LOOM_DATABASE_URL": "postgresql://x",
                "LOOM_LLM_PROVIDER": provider,
                env_key: "the-key",
                "LOOM_EMBEDDING_PROVIDER": "none",
            }
        )
        assert config.llm_api_key == "the-key"
        assert config.llm_provider == provider


def test_config_allows_skip_llm_without_api_key():
    config = LoomConfig.from_env(
        {"LOOM_DATABASE_URL": "postgresql://x", "LOOM_LLM_PROVIDER": "skip"}
    )
    assert config.llm_provider == "skip"
    assert config.llm_api_key is None
    assert config.llm_model_id is None
    assert config.llm_enabled is False


def test_config_embedding_degrades_without_gemini_key():
    config = LoomConfig.from_env(
        {
            "LOOM_DATABASE_URL": "postgresql://x",
            "LOOM_EMBEDDING_PROVIDER": "gemini",
            "LOOM_LLM_PROVIDER": "skip",
        }
    )
    assert config.embedding_provider == "gemini"
    assert config.embedding_api_key is None
    assert config.embedding_enabled is False


def test_config_requires_internal_tokens_in_production():
    with pytest.raises(ConfigError) as exc:
        LoomConfig.from_env(
            {
                "LOOM_DATABASE_URL": "postgresql://x",
                "LOOM_ENV": "production",
                "LOOM_API_KEY": "present",
                "LOOM_LLM_PROVIDER": "skip",
            }
        )
    assert "LOOM_INTERNAL_API_TOKEN" in str(exc.value)


def test_config_requires_both_slack_tokens():
    with pytest.raises(ConfigError) as exc:
        LoomConfig.from_env(
            {"LOOM_DATABASE_URL": "postgresql://x", "LOOM_SLACK_BOT_TOKEN": "xoxb-only"}
        )
    assert "Slack" in str(exc.value)


def test_config_invalid_integer_raises():
    with pytest.raises(ConfigError) as exc:
        LoomConfig.from_env(
            {"LOOM_DATABASE_URL": "postgresql://x", "LOOM_API_PORT": "not-a-number"}
        )
    assert "LOOM_API_PORT" in str(exc.value)


def test_config_never_logs_full_secret():
    secret = "super-secret-token-value-1234567890"
    redacted = redact_secret(secret)
    assert secret not in redacted
    assert redacted.startswith("super-")

    config = LoomConfig.from_env(
        {
            "LOOM_DATABASE_URL": "postgresql://user:password@host/db",
            "LOOM_API_KEY": secret,
            "LOOM_LLM_PROVIDER": "skip",
        }
    )
    summary = config.safe_summary()
    assert secret not in str(summary)
    assert "password" not in str(summary)
