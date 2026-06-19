"""Slack silent-capture: buffering + LLM gatekeeper.

A per-thread buffer accumulates messages. After the thread goes idle, a
gatekeeper LLM decides whether the conversation is worth remembering. The raw
blob is always saved before the LLM call. Nothing here may crash the worker.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog

from loom.errors import LLMError
from loom.llm.prompts import GATEKEEPER_SYSTEM_PROMPT
from loom.tasks import create_logged_task
from loom.utils import slugify

if TYPE_CHECKING:
    from loom.config import LoomConfig
    from loom.llm.router import LLMRouter
    from loom.slack.client import LoomAPIClient

logger = structlog.get_logger("loom.slack.capture")

_SUMMARY_MAX_CHARS = 500
_DOMAIN_MAX_CHARS = 50


@dataclass(frozen=True)
class GatekeeperResult:
    action: Literal["context", "context_new", "nothing"]
    domain: str = "general"
    summary: str = ""


def parse_gatekeeper_output(text: str) -> GatekeeperResult:
    """Parse the gatekeeper LLM output into a typed result.

    Invalid output is treated as ``nothing``.
    """
    cleaned = (text or "").strip()

    def _split(rest: str) -> tuple[str, str]:
        if "|" in rest:
            domain_part, summary_part = rest.split("|", 1)
        else:
            domain_part, summary_part = "general", rest
        domain = slugify(domain_part.strip() or "general", max_chars=_DOMAIN_MAX_CHARS)
        domain = domain or "general"
        summary = summary_part.strip()[:_SUMMARY_MAX_CHARS]
        return domain, summary

    if cleaned.startswith("CONTEXT_NEW:"):
        domain, summary = _split(cleaned[len("CONTEXT_NEW:"):])
        return GatekeeperResult("context_new", domain, summary)
    if cleaned.startswith("CONTEXT:"):
        domain, summary = _split(cleaned[len("CONTEXT:"):])
        return GatekeeperResult("context", domain, summary)
    if cleaned.upper().startswith("NOTHING"):
        return GatekeeperResult("nothing")

    logger.warning("gatekeeper_invalid_output")
    return GatekeeperResult("nothing")


class ConversationBuffer:
    def __init__(
        self,
        config: LoomConfig,
        api_client: LoomAPIClient,
        llm_router: LLMRouter,
    ):
        self.config = config
        self.api_client = api_client
        self.llm_router = llm_router
        self._buffers: dict[str, deque] = {}
        self._eval_tasks: dict[str, asyncio.Task] = {}
        self._last_eval: dict[str, float] = {}

    @staticmethod
    def _key(workspace_id: str, channel: str, thread_ts: str) -> str:
        return f"{workspace_id}:{channel}:{thread_ts}"

    async def add_message(
        self,
        *,
        workspace_id: str,
        channel: str,
        thread_ts: str,
        user: str,
        text: str,
        ts: str,
        is_bot: bool = False,
    ) -> None:
        # MVP: ignore bot messages to avoid feedback loops.
        if is_bot:
            return
        if not text or not text.strip():
            return

        key = self._key(workspace_id, channel, thread_ts)
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = deque(maxlen=self.config.slack_buffer_max_messages)
            self._buffers[key] = buffer
        buffer.append(
            {
                "user": user,
                "text": text,
                "ts": ts,
                "thread_ts": thread_ts,
                "is_bot": is_bot,
            }
        )

        # A new message cancels and reschedules the idle evaluation.
        existing = self._eval_tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()

        self._eval_tasks[key] = create_logged_task(
            self._idle_then_evaluate(
                workspace_id=workspace_id, channel=channel, thread_ts=thread_ts
            ),
            logger=logger,
            name=f"gatekeeper_idle:{key}",
        )

    async def _idle_then_evaluate(
        self, *, workspace_id: str, channel: str, thread_ts: str
    ) -> None:
        await asyncio.sleep(self.config.gatekeeper_idle_seconds)
        await self.force_evaluate(
            workspace_id=workspace_id, channel=channel, thread_ts=thread_ts
        )

    def _debounced(self, key: str) -> bool:
        last = self._last_eval.get(key)
        if last is None:
            return False
        return (time.monotonic() - last) < self.config.gatekeeper_debounce_seconds

    async def force_evaluate(
        self, *, workspace_id: str, channel: str, thread_ts: str
    ) -> GatekeeperResult | None:
        key = self._key(workspace_id, channel, thread_ts)
        try:
            if self._debounced(key):
                logger.debug("gatekeeper_debounced", key=key)
                return None

            buffer = self._buffers.get(key)
            if not buffer:
                return None
            messages = list(buffer)

            # Mark evaluated up-front so concurrent triggers debounce.
            self._last_eval[key] = time.monotonic()

            # Always persist the raw blob before invoking the LLM.
            await self._save_blob(workspace_id, channel, thread_ts, messages)

            if self.config.llm_provider == "skip" or not self.config.llm_api_key:
                logger.debug("gatekeeper_llm_disabled")
                return None

            user_prompt = self._build_user_prompt(messages)
            try:
                response = await self.llm_router.complete(
                    GATEKEEPER_SYSTEM_PROMPT, user_prompt, max_tokens=200
                )
            except LLMError as exc:
                logger.warning("gatekeeper_llm_failed", error_type=type(exc).__name__)
                return None

            result = parse_gatekeeper_output(response.text)
            if result.action == "nothing":
                return result

            await self._save_summary(
                workspace_id, channel, thread_ts, messages, result
            )
            return result
        except Exception as exc:  # noqa: BLE001 - capture must never crash worker
            logger.error("gatekeeper_unexpected_error", error_type=type(exc).__name__)
            return None

    async def _save_blob(
        self, workspace_id: str, channel: str, thread_ts: str, messages: list[dict]
    ) -> None:
        try:
            await self.api_client.save_conversation_blob(
                {
                    "workspace_id": workspace_id,
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "messages": messages,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("save_blob_failed", error_type=type(exc).__name__)

    async def _save_summary(
        self,
        workspace_id: str,
        channel: str,
        thread_ts: str,
        messages: list[dict],
        result: GatekeeperResult,
    ) -> None:
        participants = sorted({m["user"] for m in messages if m.get("user")})
        try:
            await self.api_client.save_context_summary(
                {
                    "workspace_id": workspace_id,
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "summary": result.summary,
                    "domain": result.domain,
                    "participants": participants,
                    "message_count": len(messages),
                    "is_new_topic": result.action == "context_new",
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("save_summary_failed", error_type=type(exc).__name__)

    @staticmethod
    def _build_user_prompt(messages: list[dict]) -> str:
        lines = ["Slack conversation:"]
        for msg in messages:
            lines.append(f"[{msg.get('ts')}] <{msg.get('user')}> {msg.get('text')}")
        return "\n".join(lines)

    async def shutdown(self) -> None:
        for task in self._eval_tasks.values():
            if not task.done():
                task.cancel()
        self._eval_tasks.clear()
