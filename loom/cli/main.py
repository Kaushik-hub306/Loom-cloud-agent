"""Loom command-line interface (Click + Rich).

Commands prefer the running API service when reachable and fall back to direct
database access otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import click
import httpx
import structlog
from rich.console import Console
from rich.table import Table

from loom import constants
from loom.config import LoomConfig
from loom.errors import ConfigError, LoomError, SlackConfigError

console = Console()
err_console = Console(stderr=True)
logger = structlog.get_logger("loom.cli")

_DEFAULT_MCP_CONFIG_PATH = "~/.claude/loom-mcp-config.json"


def _load_config() -> LoomConfig:
    try:
        return LoomConfig.from_env()
    except ConfigError as exc:
        err_console.print(f"[red]Configuration error:[/red] {exc.user_message}")
        raise SystemExit(2) from exc


# ---------------------------------------------------------------------------
# Direct store helpers
# ---------------------------------------------------------------------------
async def _with_store(config: LoomConfig, fn):
    from loom.db import DatabasePool
    from loom.embeddings import EmbeddingService
    from loom.memory.store import MemoryStore

    pool = DatabasePool(config, mode="async")
    await pool.init_async()
    try:
        store = MemoryStore(pool, EmbeddingService(config), config)
        return await fn(store)
    finally:
        await pool.close_async()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def _public_headers(config: LoomConfig) -> dict[str, str]:
    return {"X-Loom-Api-Key": config.api_key} if config.api_key else {}


def _api_reachable(config: LoomConfig) -> bool:
    try:
        resp = httpx.get(
            f"{config.api_base_url.rstrip('/')}/health", timeout=2.0
        )
        return resp.status_code < 500
    except httpx.HTTPError:
        return False


def _api_request(config: LoomConfig, method: str, path: str, **kwargs) -> dict:
    url = f"{config.api_base_url.rstrip('/')}{path}"
    headers = _public_headers(config)
    resp = httpx.request(
        method, url, headers=headers, timeout=config.http_timeout_seconds, **kwargs
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------
@click.group()
@click.version_option(version=constants.APP_VERSION, prog_name="loom")
def cli() -> None:
    """Loom: shared memory layer for AI coding agents."""


# ---------------------------------------------------------------------------
# loom init
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--env-path", default=".env", type=click.Path(), show_default=True)
@click.option(
    "--mcp-config-path", default=_DEFAULT_MCP_CONFIG_PATH, type=click.Path(),
    show_default=True,
)
@click.option("--non-interactive", is_flag=True, help="Read values from env only.")
@click.option("--force", is_flag=True, help="Overwrite existing files.")
def init(env_path: str, mcp_config_path: str, non_interactive: bool, force: bool) -> None:
    """Polished first-run setup wizard."""
    values: dict[str, str] = {}
    if non_interactive:
        config = _load_config()
        values = _config_to_env_values(config)
    else:
        values = _interactive_collect()
        try:
            config = LoomConfig.from_env(_env_with_defaults(values))
        except ConfigError as exc:
            err_console.print(f"[red]Configuration invalid:[/red] {exc.user_message}")
            raise SystemExit(2) from exc

    status = asyncio.run(config.validate_credentials(strict=True))
    if not status.ok:
        err_console.print("[red]Setup validation failed:[/red]")
        for check in status.as_list():
            if check.status == "fail":
                err_console.print(f"  - {check.name}: {check.message}")
        err_console.print("No files were written. Fix the issues and re-run `loom init`.")
        raise SystemExit(1)

    env_target = Path(env_path)
    if env_target.exists() and not force:
        if non_interactive or not click.confirm(f"{env_path} exists. Overwrite?"):
            err_console.print("Aborted: existing .env not overwritten.")
            raise SystemExit(1)

    _atomic_write(env_target, _render_env_file(values))
    mcp_target = Path(os.path.expanduser(mcp_config_path))
    _atomic_write(mcp_target, _render_mcp_config(config))

    console.print("\n[green]Loom is ready.[/green]\n")
    console.print(f"  .env written to {env_target}")
    console.print(f"  MCP config written to {mcp_target}")
    console.print("\nStart the API:   loom serve")
    console.print("Start Slack:     loom slack")
    console.print("Run tests:       loom test")
    console.print(f"Add to Claude:   copy config from {mcp_target}")


def _interactive_collect() -> dict[str, str]:
    console.print("[bold]Step 1/4 Database[/bold]")
    db_url = click.prompt("  Postgres/Supabase connection URI", hide_input=False)
    console.print("[bold]Step 2/4 LLM Provider[/bold]")
    provider = click.prompt(
        "  Provider", type=click.Choice(["deepseek", "gemini", "claude", "openai", "skip"]),
        default="skip",
    )
    values = {"LOOM_DATABASE_URL": db_url, "LOOM_LLM_PROVIDER": provider}
    if provider != "skip":
        from loom.config import PROVIDER_KEY_ENV

        key = click.prompt(f"  {provider} API key", hide_input=True, default="")
        if key:
            values[PROVIDER_KEY_ENV[provider]] = key
    console.print("[bold]Step 3/4 Slack (optional)[/bold]")
    if click.confirm("  Set up Slack bot?", default=False):
        values["LOOM_SLACK_BOT_TOKEN"] = click.prompt("  Bot token (xoxb-...)", hide_input=True)
        values["LOOM_SLACK_APP_TOKEN"] = click.prompt("  App token (xapp-...)", hide_input=True)
    gem = click.prompt(
        "  Gemini API key for embeddings (blank to skip)", hide_input=True, default=""
    )
    if gem:
        values["GEMINI_API_KEY"] = gem
        values["LOOM_EMBEDDING_PROVIDER"] = "gemini"
    else:
        values["LOOM_EMBEDDING_PROVIDER"] = "none"
    return values


def _env_with_defaults(values: dict[str, str]) -> dict[str, str]:
    env = dict(values)
    env.setdefault("LOOM_DATABASE_URL", values.get("LOOM_DATABASE_URL", ""))
    return env


def _config_to_env_values(config: LoomConfig) -> dict[str, str]:
    values = {
        "LOOM_DATABASE_URL": config.database_url,
        "LOOM_ENV": config.env,
        "LOOM_LLM_PROVIDER": config.llm_provider,
        "LOOM_EMBEDDING_PROVIDER": config.embedding_provider,
    }
    if config.slack_bot_token and config.slack_app_token:
        values["LOOM_SLACK_BOT_TOKEN"] = config.slack_bot_token
        values["LOOM_SLACK_APP_TOKEN"] = config.slack_app_token
    if config.api_key:
        values["LOOM_API_KEY"] = config.api_key
    if config.internal_api_token:
        values["LOOM_INTERNAL_API_TOKEN"] = config.internal_api_token
    if config.llm_api_key:
        from loom.config import PROVIDER_KEY_ENV

        env_key = PROVIDER_KEY_ENV.get(config.llm_provider)
        if env_key:
            values[env_key] = config.llm_api_key
    if config.embedding_api_key:
        values["GEMINI_API_KEY"] = config.embedding_api_key
    return values


def _render_env_file(values: dict[str, str]) -> str:
    lines = [
        "# Generated by `loom init`. Do not commit real secrets.",
        "# Slack is optional; Loom works as MCP-only without it.",
        "# Embeddings default to Gemini; without GEMINI_API_KEY Loom uses text search.",
        "",
    ]
    for key, val in values.items():
        lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n"


def _render_mcp_config(config: LoomConfig) -> str:
    env: dict[str, str] = {"LOOM_DATABASE_URL": config.database_url}
    if config.embedding_provider == "gemini" and config.embedding_api_key:
        env["GEMINI_API_KEY"] = config.embedding_api_key
    payload = {
        "mcpServers": {
            "loom": {
                "command": "python3",
                "args": ["-m", "loom.mcp.server"],
                "env": env,
            }
        }
    }
    return json.dumps(payload, indent=2) + "\n"


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# loom serve
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
@click.option("--reload/--no-reload", default=False)
def serve(host: str | None, port: int | None, reload: bool) -> None:
    """Start the FastAPI memory service."""
    config = _load_config()
    import uvicorn

    from loom.api.app import create_app

    bind_host = host or config.api_host
    bind_port = port or config.api_port
    console.print(
        f"[green]Loom API[/green] starting on http://{bind_host}:{bind_port} "
        f"(health: http://{bind_host}:{bind_port}/health)"
    )
    if reload:
        uvicorn.run(
            "loom.api.app:create_app", factory=True, host=bind_host, port=bind_port,
            reload=True,
        )
    else:
        uvicorn.run(create_app(config), host=bind_host, port=bind_port)


# ---------------------------------------------------------------------------
# loom slack
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--silent/--interactive", "silent", default=None)
def slack(silent: bool | None) -> None:
    """Start the Slack Socket Mode worker."""
    config = _load_config()
    if silent is not None:
        config = config.with_overrides(slack_silent=silent)
    from loom.slack.bot import run_slack_bot

    mode = "silent observer" if config.slack_silent else "interactive"
    console.print(f"[green]Loom Slack worker[/green] ({mode}) -> {config.api_base_url}")
    try:
        asyncio.run(run_slack_bot(config))
    except SlackConfigError as exc:
        err_console.print(f"[red]Slack not configured:[/red] {exc.user_message}")
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# loom status
# ---------------------------------------------------------------------------
@cli.command()
def status() -> None:
    """Print a status table for all Loom components."""
    config = _load_config()
    table = Table(title="Loom Status")
    table.add_column("Component")
    table.add_column("Status")

    db_status, rules, contexts = asyncio.run(_status_db(config))
    table.add_row("Database", db_status)

    if _api_reachable(config):
        table.add_row("API", f"running on {config.api_base_url}")
    else:
        table.add_row("API", "not reachable")

    if config.slack_configured:
        mode = "silent mode" if config.slack_silent else "interactive mode"
        table.add_row("Slack bot", f"configured ({mode})")
    else:
        table.add_row("Slack bot", "not configured")

    table.add_row("Rules stored", str(rules))
    table.add_row("Contexts", f"{contexts} active")
    console.print(table)


async def _status_db(config: LoomConfig) -> tuple[str, int, int]:
    from loom.db import DatabasePool

    pool = DatabasePool(config, mode="async")
    try:
        await pool.init_async()
        health = await pool.health_check_async()
        rules = await pool.fetchval("SELECT COUNT(*) FROM memories")
        contexts = await pool.fetchval(
            "SELECT COUNT(*) FROM conversation_contexts WHERE expires_at > NOW()"
        )
        latency = health.get("latency_ms")
        status_text = f"connected ({latency}ms)" if health.get("connected") else "error"
        return status_text, int(rules or 0), int(contexts or 0)
    except LoomError as exc:
        return f"error: {exc.user_message}", 0, 0
    finally:
        await pool.close_async()


# ---------------------------------------------------------------------------
# loom teach
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("domain")
@click.argument("rule_type")
@click.argument("rule")
@click.option("--example", default="")
@click.option("--confidence", default=7, type=click.IntRange(1, 10))
def teach(domain: str, rule_type: str, rule: str, example: str, confidence: int) -> None:
    """Teach Loom a durable rule."""
    config = _load_config()
    if _api_reachable(config):
        result = _api_request(
            config, "POST", "/teach",
            json={"domain": domain, "rule_type": rule_type, "rule": rule,
                  "example": example, "confidence": confidence},
        )
        console.print(f"[green]{result.get('message', 'Remembered.')}[/green]")
        console.print(f"ID: {result.get('id')}")
        return

    async def _do(store):
        return await store.teach(domain, rule_type, rule, example=example, confidence=confidence)

    result = asyncio.run(_with_store(config, _do))
    verb = "Updated" if result.is_update else "Remembered"
    console.print(f"[green]{verb} {result.memory.domain} {result.memory.rule_type}.[/green]")
    console.print(f"ID: {result.memory.id}")


# ---------------------------------------------------------------------------
# loom recall
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("query")
@click.option("--domain", default=None)
@click.option("--limit", default=10, type=click.IntRange(1, 30))
def recall(query: str, domain: str | None, limit: int) -> None:
    """Recall rules and recent context from Loom."""
    config = _load_config()
    from loom.memory.formatting import confidence_label

    if _api_reachable(config):
        result = _api_request(
            config, "POST", "/recall",
            json={"query": query, "domain": domain, "limit": limit},
        )
        memories = result.get("memories", [])
        contexts = result.get("contexts", [])
        _print_recall(memories, contexts, confidence_label)
        return

    async def _do(store):
        mems = await store.recall(query, domain=domain, limit=limit)
        ctxs = await store.search_contexts(query, limit=min(limit, 5))
        return mems, ctxs

    memories, contexts = asyncio.run(_with_store(config, _do))
    _print_recall(
        [{"domain": m.domain, "rule_type": m.rule_type, "rule": m.rule,
          "confidence": m.confidence, "uses": m.uses} for m in memories],
        [{"domain": c.domain, "summary": c.summary} for c in contexts],
        confidence_label,
    )


def _print_recall(memories, contexts, confidence_label) -> None:
    if not memories and not contexts:
        console.print("No results.")
        return
    if memories:
        console.print("[bold]Rules:[/bold]")
        for m in memories:
            label = confidence_label(int(m.get("confidence", 5)))
            console.print(f"- [{label}] {m.get('domain')}/{m.get('rule_type')}: {m.get('rule')}")
    if contexts:
        console.print("\n[bold]Recent conversations:[/bold]")
        for c in contexts:
            console.print(f"- [{c.get('domain')}] {c.get('summary')}")


# ---------------------------------------------------------------------------
# loom export / import
# ---------------------------------------------------------------------------
@cli.command(name="export")
@click.option("--include-embeddings", is_flag=True)
def export_cmd(include_embeddings: bool) -> None:
    """Export memories as JSON to stdout."""
    config = _load_config()
    if _api_reachable(config):
        payload = _api_request(
            config, "GET", "/export",
            params={"include_embeddings": str(include_embeddings).lower()},
        )
    else:
        async def _do(store):
            return await store.export_memories(include_embeddings=include_embeddings)

        payload = asyncio.run(_with_store(config, _do))
    # JSON to stdout only; logs/status to stderr.
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


@cli.command(name="import")
@click.argument("path", type=click.Path(exists=True))
@click.option("--no-regenerate-embeddings", is_flag=True)
def import_cmd(path: str, no_regenerate_embeddings: bool) -> None:
    """Import memories from a JSON export file."""
    config = _load_config()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    regenerate = not no_regenerate_embeddings

    if _api_reachable(config):
        result = _api_request(
            config, "POST", "/import",
            params={"regenerate_embeddings": str(regenerate).lower()},
            json=payload,
        )
    else:
        async def _do(store):
            return await store.import_memories(payload, regenerate_embeddings=regenerate)

        result = asyncio.run(_with_store(config, _do))
    err_console.print(
        f"[green]Import complete:[/green] imported={result.get('imported')} "
        f"updated={result.get('updated')} skipped={result.get('skipped')} "
        f"failed={result.get('failed')}"
    )


# ---------------------------------------------------------------------------
# loom test
# ---------------------------------------------------------------------------
@cli.command(
    name="test",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
def test_cmd(pytest_args: tuple[str, ...]) -> None:
    """Run the test suite (pass extra args after --)."""
    cmd = [sys.executable, "-m", "pytest", *pytest_args]
    result = subprocess.run(cmd, check=False)  # noqa: S603
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    cli()
