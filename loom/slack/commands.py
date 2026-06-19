"""Slash command handlers: /loom-teach, /loom-stats, /loom-recall.

Each handler is a plain async function taking explicit dependencies so it can be
unit-tested without the Slack runtime. ``respond`` is an async callable that
sends an ephemeral message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from loom.errors import LoomError

if TYPE_CHECKING:
    from loom.slack.client import LoomAPIClient

logger = structlog.get_logger("loom.slack.commands")

Respond = Callable[[str], Awaitable[None]]

_RECALL_LIMIT = 5


def parse_teach_command(text: str) -> tuple[str, str, str]:
    """Parse ``domain[:rule_type] rest...`` into (domain, rule_type, rule)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Usage: /loom-teach domain:rule_type the rule text")
    parts = text.split(maxsplit=1)
    head = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    if ":" in head:
        domain, rule_type = head.split(":", 1)
        domain = domain.strip()
        rule_type = rule_type.strip() or "convention"
    else:
        domain = head.strip()
        rule_type = "convention"

    if not domain:
        raise ValueError("A domain is required, e.g. coding:convention.")
    if len(rest) < 5:
        raise ValueError("The rule text must be at least 5 characters.")
    return domain, rule_type, rest


async def handle_teach_command(
    text: str, api_client: LoomAPIClient, respond: Respond
) -> None:
    try:
        domain, rule_type, rule = parse_teach_command(text)
    except ValueError as exc:
        await respond(f"Could not teach: {exc}")
        return
    try:
        result = await api_client.teach(
            {"domain": domain, "rule_type": rule_type, "rule": rule}
        )
    except LoomError as exc:
        await respond(f"Could not save rule: {exc.user_message}")
        return
    await respond(result.get("message", f"Remembered {domain} {rule_type}."))


async def handle_stats_command(api_client: LoomAPIClient, respond: Respond) -> None:
    try:
        stats = await api_client.stats()
    except LoomError as exc:
        await respond(f"Could not load stats: {exc.user_message}")
        return
    lines = [
        "Loom memory stats",
        f"Rules: {stats.get('total_rules', 0)}",
        f"Contexts: {stats.get('active_contexts', 0)} active",
        f"Blobs: {stats.get('active_blobs', 0)} active",
    ]
    domains = stats.get("rules_by_domain", {})
    if domains:
        lines.append("Top domains:")
        for domain, count in sorted(domains.items(), key=lambda kv: kv[1], reverse=True)[:5]:
            lines.append(f"- {domain}: {count}")
    await respond("\n".join(lines))


async def handle_recall_command(
    text: str,
    channel: str,
    workspace_id: str,
    api_client: LoomAPIClient,
    respond: Respond,
) -> None:
    query = (text or "").strip()
    if not query:
        await respond("Usage: /loom-recall your search terms")
        return
    try:
        result = await api_client.recall(
            {
                "query": query,
                "channel": channel,
                "workspace_id": workspace_id,
                "limit": _RECALL_LIMIT,
                "include_contexts": True,
            }
        )
    except LoomError as exc:
        await respond(f"Could not recall: {exc.user_message}")
        return

    memories = result.get("memories", [])
    contexts = result.get("contexts", [])
    if not memories and not contexts:
        await respond(f"No Loom results for: {query}")
        return

    lines = [f"Loom recall results for: {query}", ""]
    if memories:
        lines.append("Rules:")
        for m in memories:
            lines.append(f"- [{m.get('domain')}/{m.get('rule_type')}] {m.get('rule')}")
    if contexts:
        lines.append("")
        lines.append("Recent conversations:")
        for c in contexts:
            lines.append(f"- [{c.get('domain')}] {c.get('summary')}")
    await respond("\n".join(lines))
