"""Phase 5 Slack capture and handler tests (no real Slack/LLM/network)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from loom.config import LoomConfig
from loom.llm.router import LLMResponse
from loom.slack import bot
from loom.slack.capture import ConversationBuffer, parse_gatekeeper_output


def make_config(**overrides) -> LoomConfig:
    base = LoomConfig(
        database_url="postgresql://localhost/none",
        env="test",
        llm_provider="deepseek",
        llm_api_key="test-key",
        embedding_provider="none",
        slack_silent=True,
        gatekeeper_idle_seconds=60,
        gatekeeper_debounce_seconds=180,
        slack_buffer_max_messages=30,
    )
    return replace(base, **overrides)


def make_buffer(config=None, *, llm_text="NOTHING", llm_raises=False):
    config = config or make_config()
    api_client = AsyncMock()
    api_client.save_conversation_blob = AsyncMock(return_value={"id": "b", "saved": True})
    api_client.save_context_summary = AsyncMock(return_value={"id": "c", "saved": True})
    llm_router = AsyncMock()
    if llm_raises:
        from loom.errors import LLMError

        llm_router.complete = AsyncMock(side_effect=LLMError("boom"))
    else:
        llm_router.complete = AsyncMock(
            return_value=LLMResponse(text=llm_text, model_used="m", usage=None)
        )
    buffer = ConversationBuffer(config, api_client, llm_router)
    return buffer, api_client, llm_router


async def _add(buffer, text, user="U1", ts="1.0", channel="C1", thread="T1"):
    await buffer.add_message(
        workspace_id="W", channel=channel, thread_ts=thread, user=user, text=text, ts=ts
    )


# --------------------------------------------------------------------------
# Buffer behavior
# --------------------------------------------------------------------------
async def test_buffer_resets_timer_on_new_message():
    buffer, *_ = make_buffer(make_config(gatekeeper_idle_seconds=5))
    await _add(buffer, "first")
    key = buffer._key("W", "C1", "T1")
    first_task = buffer._eval_tasks[key]
    await _add(buffer, "second")
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()
    assert buffer._eval_tasks[key] is not first_task
    await buffer.shutdown()


async def test_buffer_keeps_max_30_messages():
    buffer, *_ = make_buffer(make_config(slack_buffer_max_messages=30))
    for i in range(35):
        await _add(buffer, f"msg {i}", ts=str(i))
    key = buffer._key("W", "C1", "T1")
    assert len(buffer._buffers[key]) == 30
    await buffer.shutdown()


# --------------------------------------------------------------------------
# Gatekeeper behavior
# --------------------------------------------------------------------------
async def test_gatekeeper_saves_blob_before_llm_call():
    buffer, api_client, llm_router = make_buffer(llm_text="NOTHING")
    await _add(buffer, "let us discuss the auth design")
    await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert api_client.save_conversation_blob.await_count == 1
    assert llm_router.complete.await_count == 1


async def test_gatekeeper_saves_context_on_context_response():
    buffer, api_client, _ = make_buffer(
        llm_text="CONTEXT: architecture | We chose async asyncpg for IO."
    )
    await _add(buffer, "discussion about async io")
    result = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert result.action == "context"
    args = api_client.save_context_summary.await_args.args[0]
    assert args["is_new_topic"] is False
    assert args["domain"] == "architecture"


async def test_gatekeeper_saves_new_context_on_context_new_response():
    buffer, api_client, _ = make_buffer(
        llm_text="CONTEXT_NEW: testing | We now require deterministic fixtures."
    )
    await _add(buffer, "switching topic to testing")
    result = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert result.action == "context_new"
    args = api_client.save_context_summary.await_args.args[0]
    assert args["is_new_topic"] is True


async def test_gatekeeper_saves_nothing_on_nothing_response():
    buffer, api_client, _ = make_buffer(llm_text="NOTHING")
    await _add(buffer, "hi there")
    result = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert result.action == "nothing"
    assert api_client.save_context_summary.await_count == 0


async def test_gatekeeper_debounce_skips_llm_but_still_saves_blob():
    buffer, api_client, llm_router = make_buffer(
        make_config(gatekeeper_debounce_seconds=180), llm_text="NOTHING"
    )
    await _add(buffer, "first message about a topic")
    await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert llm_router.complete.await_count == 1
    second = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert second is None
    assert api_client.save_conversation_blob.await_count == 2
    assert llm_router.complete.await_count == 1  # not called again


async def test_gatekeeper_retries_after_blob_save_rejected():
    buffer, api_client, llm_router = make_buffer(llm_text="NOTHING")
    api_client.save_conversation_blob.side_effect = [
        {"id": "", "saved": False},
        {"id": "b", "saved": True},
    ]
    await _add(buffer, "first message about a topic")

    first = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert first is None
    assert llm_router.complete.await_count == 0

    second = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert second.action == "nothing"
    assert api_client.save_conversation_blob.await_count == 2
    assert llm_router.complete.await_count == 1


async def test_gatekeeper_treats_summary_save_rejected_as_failure():
    buffer, api_client, _ = make_buffer(
        llm_text="CONTEXT: architecture | We chose async asyncpg for IO."
    )
    api_client.save_context_summary.return_value = {"id": "", "saved": False}
    await _add(buffer, "discussion about async io")

    result = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert result is None
    assert api_client.save_context_summary.await_count == 1


async def test_gatekeeper_survives_llm_failure():
    buffer, api_client, _ = make_buffer(llm_raises=True)
    await _add(buffer, "some real discussion here")
    result = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert result is None
    assert api_client.save_conversation_blob.await_count == 1  # blob still saved


async def test_gatekeeper_invalid_llm_output_treated_as_nothing():
    buffer, api_client, _ = make_buffer(llm_text="totally invalid output")
    await _add(buffer, "discuss something")
    result = await buffer.force_evaluate(workspace_id="W", channel="C1", thread_ts="T1")
    assert result.action == "nothing"
    assert api_client.save_context_summary.await_count == 0


def test_parse_gatekeeper_variants():
    assert parse_gatekeeper_output("NOTHING").action == "nothing"
    r = parse_gatekeeper_output("CONTEXT: coding | use async")
    assert r.action == "context" and r.domain == "coding"
    r2 = parse_gatekeeper_output("CONTEXT_NEW: Security Stuff | rotate keys")
    assert r2.action == "context_new" and r2.domain == "security-stuff"


# --------------------------------------------------------------------------
# Action handlers
# --------------------------------------------------------------------------
async def test_approve_learning_calls_teach_endpoint():
    api_client = AsyncMock()
    api_client.teach = AsyncMock(return_value={"message": "Remembered."})
    body = {
        "actions": [
            {"value": '{"domain":"coding","rule_type":"convention","rule":"use async"}'}
        ],
        "channel": {"id": "C1"},
        "message": {"ts": "1.0"},
    }
    updated = AsyncMock()
    await bot.handle_approve_learning(
        body, api_client=api_client, update_message=updated
    )
    assert api_client.teach.await_count == 1
    updated.assert_awaited_once()


async def test_dismiss_learning_does_not_call_teach():
    api_client = AsyncMock()
    api_client.teach = AsyncMock()
    updated = AsyncMock()
    await bot.handle_dismiss_learning({"actions": []}, update_message=updated)
    assert api_client.teach.await_count == 0
    updated.assert_awaited_once()


# --------------------------------------------------------------------------
# Message handling / silent mode
# --------------------------------------------------------------------------
async def test_silent_mode_never_calls_say_for_channel_messages():
    config = make_config(slack_silent=True)
    buffer, api_client, _ = make_buffer(config)
    say = AsyncMock()
    event = {
        "type": "message",
        "channel": "C1",
        "channel_type": "channel",
        "user": "U1",
        "text": "hello team",
        "ts": "1.0",
    }
    await bot.process_message_event(
        event, config=config, buffer=buffer, api_client=api_client, say=say,
        workspace_id="W",
    )
    say.assert_not_awaited()
    key = buffer._key("W", "C1", "1.0")
    assert key in buffer._buffers
    await buffer.shutdown()


async def test_loom_recall_calls_recall_endpoint_not_session_init():
    from loom.slack.commands import handle_recall_command

    api_client = AsyncMock()
    api_client.recall = AsyncMock(return_value={"memories": [], "contexts": []})
    responses: list[str] = []

    async def respond(text: str) -> None:
        responses.append(text)

    await handle_recall_command(
        "auth middleware", "C1", "W", api_client, respond
    )
    assert api_client.recall.await_count == 1
    assert not hasattr(api_client, "session_init") or api_client.session_init.await_count == 0
    assert responses


# --------------------------------------------------------------------------
# Import boundary
# --------------------------------------------------------------------------
def test_slack_worker_does_not_import_db_or_api():
    import ast

    slack_dir = Path(bot.__file__).parent
    forbidden = ("loom.api", "loom.db", "loom.memory.store")
    for py_file in slack_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for token in forbidden:
            assert not any(
                mod == token or mod.startswith(token + ".") for mod in imported
            ), f"{py_file.name} must not import {token}"
