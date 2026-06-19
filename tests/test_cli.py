"""Phase 6 CLI tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from click.testing import CliRunner

from loom.cli import main as cli_main
from loom.config import CredentialCheck, CredentialStatus, LoomConfig
from tests.conftest import TEST_DATABASE_URL, requires_db

runner = CliRunner()


def _status(**fails) -> CredentialStatus:
    def check(name: str) -> CredentialCheck:
        if name in fails:
            return CredentialCheck(name, "fail", fails[name])
        return CredentialCheck(name, "ok", "ok")

    return CredentialStatus(
        database=check("database"),
        slack_bot=check("slack_bot"),
        slack_app=check("slack_app"),
        llm=check("llm"),
        embedding=check("embedding"),
    )


def _db_env() -> dict[str, str]:
    return {
        "LOOM_DATABASE_URL": TEST_DATABASE_URL or "postgresql://localhost/none",
        "LOOM_LLM_PROVIDER": "skip",
        "LOOM_EMBEDDING_PROVIDER": "none",
        "LOOM_API_BASE_URL": "http://127.0.0.1:59999",  # unreachable -> direct DB
    }


# ---------------------------------------------------------------------------
# loom init
# ---------------------------------------------------------------------------
def test_init_writes_env_only_on_all_valid_credentials(monkeypatch):
    async def ok(self, strict=False):
        return _status()

    monkeypatch.setattr(LoomConfig, "validate_credentials", ok)
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main.cli,
            ["init", "--non-interactive", "--env-path", ".env",
             "--mcp-config-path", "mcp.json", "--force"],
            env={"LOOM_DATABASE_URL": "postgresql://localhost/db",
                 "LOOM_LLM_PROVIDER": "skip", "LOOM_EMBEDDING_PROVIDER": "none"},
        )
        assert result.exit_code == 0, result.output
        assert Path(".env").exists()
        assert Path("mcp.json").exists()


def test_init_prints_specific_failure_message_on_bad_db(monkeypatch):
    async def bad_db(self, strict=False):
        return _status(database="Cannot connect to database")

    monkeypatch.setattr(LoomConfig, "validate_credentials", bad_db)
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main.cli,
            ["init", "--non-interactive", "--env-path", ".env",
             "--mcp-config-path", "mcp.json"],
            env={"LOOM_DATABASE_URL": "postgresql://localhost/db",
                 "LOOM_LLM_PROVIDER": "skip", "LOOM_EMBEDDING_PROVIDER": "none"},
        )
        assert result.exit_code != 0
        assert "database" in result.output.lower()
        assert not Path(".env").exists()


def test_init_prints_specific_failure_message_on_bad_slack_token(monkeypatch):
    async def bad_slack(self, strict=False):
        return _status(slack_bot="Bot token invalid")

    monkeypatch.setattr(LoomConfig, "validate_credentials", bad_slack)
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main.cli,
            ["init", "--non-interactive", "--env-path", ".env",
             "--mcp-config-path", "mcp.json"],
            env={"LOOM_DATABASE_URL": "postgresql://localhost/db",
                 "LOOM_LLM_PROVIDER": "skip", "LOOM_EMBEDDING_PROVIDER": "none",
                 "LOOM_SLACK_BOT_TOKEN": "xoxb-x", "LOOM_SLACK_APP_TOKEN": "xapp-x"},
        )
        assert result.exit_code != 0
        assert "slack" in result.output.lower()
        assert not Path(".env").exists()


def test_init_writes_mcp_config_with_no_slack_secrets(monkeypatch):
    async def ok(self, strict=False):
        return _status()

    monkeypatch.setattr(LoomConfig, "validate_credentials", ok)
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main.cli,
            ["init", "--non-interactive", "--env-path", ".env",
             "--mcp-config-path", "mcp.json", "--force"],
            env={"LOOM_DATABASE_URL": "postgresql://localhost/db",
                 "LOOM_LLM_PROVIDER": "skip",
                 "LOOM_EMBEDDING_PROVIDER": "gemini", "GEMINI_API_KEY": "gem-secret",
                 "LOOM_SLACK_BOT_TOKEN": "xoxb-secret",
                 "LOOM_SLACK_APP_TOKEN": "xapp-secret"},
        )
        assert result.exit_code == 0, result.output
        mcp = json.loads(Path("mcp.json").read_text())
        env = mcp["mcpServers"]["loom"]["env"]
        assert "LOOM_DATABASE_URL" in env
        assert env.get("GEMINI_API_KEY") == "gem-secret"
        assert "xoxb-secret" not in json.dumps(mcp)
        assert "xapp-secret" not in json.dumps(mcp)


# ---------------------------------------------------------------------------
# loom slack
# ---------------------------------------------------------------------------
def test_slack_command_requires_tokens():
    result = runner.invoke(
        cli_main.cli, ["slack"],
        env={"LOOM_DATABASE_URL": "postgresql://localhost/db",
             "LOOM_LLM_PROVIDER": "skip", "LOOM_EMBEDDING_PROVIDER": "none"},
    )
    assert result.exit_code != 0
    assert "slack" in result.output.lower()


# ---------------------------------------------------------------------------
# loom test
# ---------------------------------------------------------------------------
def test_test_command_invokes_pytest(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)
    result = runner.invoke(cli_main.cli, ["test", "--", "-q"])
    assert result.exit_code == 0
    assert "pytest" in captured["cmd"]
    assert "-q" in captured["cmd"]


# ---------------------------------------------------------------------------
# DB-backed commands (integration)
# ---------------------------------------------------------------------------
@requires_db
def test_status_prints_all_components(db_pool):
    result = runner.invoke(cli_main.cli, ["status"], env=_db_env())
    assert result.exit_code == 0, result.output
    for label in ("Database", "API", "Slack", "Rules", "Contexts"):
        assert label in result.output


@requires_db
def test_teach_command_stores_rule(db_pool):
    result = runner.invoke(
        cli_main.cli,
        ["teach", "coding", "convention", "Use async/await for all I/O operations",
         "--example", "Use asyncpg in FastAPI routes."],
        env=_db_env(),
    )
    assert result.exit_code == 0, result.output
    assert "ID:" in result.output


@requires_db
def test_recall_command_returns_results(db_pool):
    runner.invoke(
        cli_main.cli,
        ["teach", "coding", "convention", "Prefer pure functions where practical"],
        env=_db_env(),
    )
    result = runner.invoke(
        cli_main.cli, ["recall", "pure functions", "--limit", "5"], env=_db_env()
    )
    assert result.exit_code == 0, result.output
    assert "pure functions" in result.output.lower()


@requires_db
def test_export_import_roundtrip_preserves_all_fields(db_pool, tmp_path):
    runner.invoke(
        cli_main.cli,
        ["teach", "security", "policy", "Rotate API keys every ninety days",
         "--example", "Use a scheduled rotation job.", "--confidence", "8"],
        env=_db_env(),
    )
    export_result = runner.invoke(cli_main.cli, ["export"], env=_db_env())
    assert export_result.exit_code == 0, export_result.output
    payload = json.loads(export_result.stdout)
    assert len(payload["memories"]) == 1
    mem = payload["memories"][0]
    assert mem["rule"] == "Rotate API keys every ninety days"
    assert mem["example"] == "Use a scheduled rotation job."
    assert mem["confidence"] == 8

    export_file = tmp_path / "backup.json"
    export_file.write_text(json.dumps(payload))
    import_result = runner.invoke(
        cli_main.cli, ["import", str(export_file), "--no-regenerate-embeddings"],
        env=_db_env(),
    )
    assert import_result.exit_code == 0, import_result.output


@requires_db
def test_serve_starts_and_health_check_passes(db_pool):
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "LOOM_DATABASE_URL": TEST_DATABASE_URL,
        "LOOM_LLM_PROVIDER": "skip",
        "LOOM_EMBEDDING_PROVIDER": "none",
        "LOOM_API_HOST": "127.0.0.1",
        "LOOM_API_PORT": str(port),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "loom", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        ok = False
        for _ in range(40):
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if resp.status_code in (200, 503):
                    ok = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        assert ok, "server did not become healthy"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
