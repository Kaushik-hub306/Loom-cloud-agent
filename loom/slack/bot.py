"""Slack Socket Mode worker.

Hard rules: this module must not import loom.api, loom.db, or loom.memory.store.
All data access happens over HTTP via ``LoomAPIClient``. The gatekeeper LLM is
used directly because it needs no database.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from loom.config import LoomConfig
from loom.errors import LoomError, SlackConfigError
from loom.llm.router import LLMRouter
from loom.logging_config import configure_logging
from loom.slack.capture import ConversationBuffer
from loom.slack.client import LoomAPIClient
from loom.slack.commands import (
    handle_recall_command,
    handle_stats_command,
    handle_teach_command,
)

logger = structlog.get_logger("loom.slack.bot")

_IGNORED_SUBTYPES = {"bot_message", "message_deleted", "channel_join", "message_changed"}
_HEALTH_POLL_BASE_SECONDS = 1.0

Say = Callable[..., Awaitable[Any]]


def _is_ignorable(event: dict, bot_user_id: str | None) -> bool:
    if event.get("subtype") in _IGNORED_SUBTYPES:
        return True
    if event.get("bot_id"):
        return True
    if not (event.get("text") or "").strip():
        return True
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    return False


async def process_message_event(
    event: dict,
    *,
    config: LoomConfig,
    buffer: ConversationBuffer,
    api_client: LoomAPIClient,
    say: Say,
    workspace_id: str = "",
    bot_user_id: str | None = None,
) -> None:
    """Handle a Slack ``message`` event.

    Silent mode: buffer only, never reply. Interactive mode: reply to DMs only.
    """
    if _is_ignorable(event, bot_user_id):
        return

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    is_dm = event.get("channel_type") == "im"

    await buffer.add_message(
        workspace_id=workspace_id,
        channel=channel,
        thread_ts=thread_ts,
        user=event.get("user", ""),
        text=event.get("text", ""),
        ts=event.get("ts", ""),
        is_bot=False,
    )

    if config.slack_silent:
        return
    if not is_dm:
        return

    await _ask_and_reply(event, config=config, api_client=api_client, say=say,
                         workspace_id=workspace_id)


async def process_app_mention(
    event: dict,
    *,
    config: LoomConfig,
    buffer: ConversationBuffer,
    api_client: LoomAPIClient,
    say: Say,
    workspace_id: str = "",
    bot_user_id: str | None = None,
) -> None:
    if not (event.get("text") or "").strip():
        return

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")

    await buffer.add_message(
        workspace_id=workspace_id,
        channel=channel,
        thread_ts=thread_ts,
        user=event.get("user", ""),
        text=event.get("text", ""),
        ts=event.get("ts", ""),
        is_bot=False,
    )

    if config.slack_silent:
        return
    await _ask_and_reply(event, config=config, api_client=api_client, say=say,
                         workspace_id=workspace_id, in_thread=True)


async def _ask_and_reply(
    event: dict,
    *,
    config: LoomConfig,
    api_client: LoomAPIClient,
    say: Say,
    workspace_id: str = "",
    in_thread: bool = False,
) -> None:
    message = (event.get("text") or "").strip()
    payload = {
        "task": message[:500] or "Answer the user's Slack question.",
        "message": message,
        "channel": event.get("channel", ""),
        "workspace_id": workspace_id,
    }
    try:
        result = await api_client.ask(payload)
    except LoomError as exc:
        logger.warning("ask_failed", error_type=type(exc).__name__)
        return
    text = result.get("response", "")
    if not text:
        return
    kwargs: dict[str, Any] = {"text": text}
    if in_thread:
        kwargs["thread_ts"] = event.get("thread_ts") or event.get("ts")
    await say(**kwargs)


async def handle_approve_learning(
    body: dict,
    *,
    api_client: LoomAPIClient,
    update_message: Callable[[str], Awaitable[None]] | None = None,
    respond: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Persist a proposed learning extracted from the action payload."""
    proposal = _extract_action_value(body)
    if not proposal:
        if respond:
            await respond("Could not read the proposed learning.")
        return
    try:
        await api_client.teach(proposal)
    except LoomError as exc:
        logger.warning("approve_learning_failed", error_type=type(exc).__name__)
        if respond:
            await respond(f"Could not remember that: {exc.user_message}")
        return
    if update_message:
        await update_message("Remembered.")


async def handle_dismiss_learning(
    body: dict,
    *,
    update_message: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Dismiss a proposed learning without storing anything."""
    if update_message:
        await update_message("Dismissed.")


def _extract_action_value(body: dict) -> dict | None:
    try:
        actions = body.get("actions") or []
        if not actions:
            return None
        raw = actions[0].get("value")
        if not raw:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        logger.warning("action_value_parse_failed", error_type=type(exc).__name__)
        return None


async def wait_for_api(
    api_client: LoomAPIClient, *, max_backoff: int, max_attempts: int | None = None
) -> bool:
    """Poll the API /health until reachable, with exponential backoff."""
    backoff = _HEALTH_POLL_BASE_SECONDS
    attempt = 0
    while max_attempts is None or attempt < max_attempts:
        try:
            health = await api_client.health()
            if health.get("status") in ("ok", "degraded"):
                logger.info("api_reachable")
                return True
        except LoomError as exc:
            logger.warning("api_not_ready", error_type=type(exc).__name__)
        await asyncio.sleep(min(backoff, max_backoff))
        backoff *= 2
        attempt += 1
    return False


async def run_slack_bot(config: LoomConfig | None = None) -> None:
    config = config or LoomConfig.from_env()
    configure_logging(config)

    if not config.slack_configured:
        raise SlackConfigError(
            "Slack requires LOOM_SLACK_BOT_TOKEN and LOOM_SLACK_APP_TOKEN. "
            "Run `loom init` to configure Slack."
        )

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    api_client = LoomAPIClient(config)
    llm_router = LLMRouter(config)
    buffer = ConversationBuffer(config, api_client, llm_router)

    await wait_for_api(
        api_client, max_backoff=config.slack_reconnect_backoff_max_seconds
    )

    app = AsyncApp(token=config.slack_bot_token, logger=None)

    try:
        auth = await app.client.auth_test()
        bot_user_id = auth.get("user_id")
        workspace_id = auth.get("team_id", "")
    except Exception as exc:  # noqa: BLE001 - degrade gracefully on auth issues
        logger.warning("auth_test_failed", error_type=type(exc).__name__)
        bot_user_id = None
        workspace_id = ""

    @app.event("message")
    async def _on_message(event, say):  # noqa: ANN001
        await process_message_event(
            event, config=config, buffer=buffer, api_client=api_client, say=say,
            workspace_id=workspace_id, bot_user_id=bot_user_id,
        )

    @app.event("app_mention")
    async def _on_mention(event, say):  # noqa: ANN001
        await process_app_mention(
            event, config=config, buffer=buffer, api_client=api_client, say=say,
            workspace_id=workspace_id, bot_user_id=bot_user_id,
        )

    @app.action("approve_learning")
    async def _on_approve(ack, body, client):  # noqa: ANN001
        await ack()

        async def _update(text: str) -> None:
            await client.chat_update(
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=text,
                blocks=[],
            )

        await handle_approve_learning(
            body, api_client=api_client, update_message=_update
        )

    @app.action("dismiss_learning")
    async def _on_dismiss(ack, body, client):  # noqa: ANN001
        await ack()

        async def _update(text: str) -> None:
            await client.chat_update(
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=text,
                blocks=[],
            )

        await handle_dismiss_learning(body, update_message=_update)

    @app.command("/loom-teach")
    async def _cmd_teach(ack, command, respond):  # noqa: ANN001
        await ack()
        await handle_teach_command(
            command.get("text", ""), api_client, _respond_adapter(respond)
        )

    @app.command("/loom-stats")
    async def _cmd_stats(ack, respond):  # noqa: ANN001
        await ack()
        await handle_stats_command(api_client, _respond_adapter(respond))

    @app.command("/loom-recall")
    async def _cmd_recall(ack, command, respond):  # noqa: ANN001
        await ack()
        await handle_recall_command(
            command.get("text", ""),
            command.get("channel_id", ""),
            workspace_id,
            api_client,
            _respond_adapter(respond),
        )

    handler = AsyncSocketModeHandler(app, config.slack_app_token)
    mode = "silent observer" if config.slack_silent else "interactive"
    logger.info("slack_worker_starting", mode=mode, api_base_url=config.api_base_url)

    try:
        await _run_with_reconnect(handler, config)
    finally:
        await buffer.shutdown()
        await api_client.aclose()


def _respond_adapter(respond) -> Callable[[str], Awaitable[None]]:  # noqa: ANN001
    async def _send(text: str) -> None:
        await respond(text)

    return _send


async def _run_with_reconnect(handler, config: LoomConfig) -> None:  # noqa: ANN001
    backoff = 1.0
    while True:
        try:
            await handler.start_async()
            return
        except Exception as exc:  # noqa: BLE001 - reconnect on transient failures
            logger.warning(
                "socket_mode_disconnected",
                error_type=type(exc).__name__,
                retry_in=round(backoff, 1),
            )
            await asyncio.sleep(min(backoff, config.slack_reconnect_backoff_max_seconds))
            backoff *= 2
