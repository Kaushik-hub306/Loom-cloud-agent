"""Deterministic prompt formatting shared by API and MCP."""

from __future__ import annotations

from loom.memory.models import ConversationContext, Memory, SessionContext

_PRIORITY_DOMAINS = ["coding", "architecture", "security", "testing"]

_HEADER = "<!-- LOOM:SESSION_CONTEXT -->"
_FOOTER = "<!-- /LOOM:SESSION_CONTEXT -->"


def confidence_label(confidence: int) -> str:
    if confidence >= 8:
        return "HIGH"
    if confidence >= 5:
        return "MED"
    return "LOW"


def _domain_title(domain: str) -> str:
    return domain.replace("-", " ").replace("_", " ").strip().title()


def _sort_domains(domains: list[str]) -> list[str]:
    priority = [d for d in _PRIORITY_DOMAINS if d in domains]
    rest = sorted(d for d in domains if d not in _PRIORITY_DOMAINS)
    return priority + rest


def _format_memories(memories: list[Memory]) -> list[str]:
    by_domain: dict[str, list[Memory]] = {}
    for memory in memories:
        by_domain.setdefault(memory.domain, []).append(memory)

    lines: list[str] = []
    for domain in _sort_domains(list(by_domain.keys())):
        # confidence desc, then uses desc, then updated_at desc.
        items = sorted(
            by_domain[domain],
            key=lambda m: (m.confidence, m.uses, m.updated_at.timestamp()),
            reverse=True,
        )
        lines.append(f"### {_domain_title(domain)}")
        for memory in items:
            label = confidence_label(memory.confidence)
            lines.append(f"- [{label}] {memory.rule_type}: {memory.rule}")
            if memory.example and memory.example.strip():
                lines.append(f"  Example: {memory.example.strip()}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _context_sort_key(context: ConversationContext):
    if context.score is not None:
        return (-context.score, -context.updated_at.timestamp())
    return (0.0, -context.updated_at.timestamp())


def _format_contexts(contexts: list[ConversationContext]) -> list[str]:
    ordered = sorted(contexts, key=_context_sort_key)
    lines = ["### Recent conversations"]
    for ctx in ordered:
        date = ctx.updated_at.strftime("%Y-%m-%d")
        lines.append(f"- [{ctx.domain}] ({date}, #{ctx.channel}): {ctx.summary}")
    return lines


def format_session_context(context: SessionContext) -> str:
    memories = context.memories
    contexts = context.contexts

    if not memories and not contexts:
        return "\n".join(
            [
                _HEADER,
                "## Team knowledge (0 rules | 0 past conversations)",
                "",
                "No conventions or recent context are stored yet. As you work, "
                "Loom can learn durable team preferences through `loom teach`, "
                "`/loom-teach`, or the MCP `teach` tool.",
                "",
                "Follow user instructions first.",
                _FOOTER,
            ]
        )

    lines: list[str] = [
        _HEADER,
        f"## Team knowledge ({len(memories)} rules | "
        f"{len(contexts)} past conversations)",
        "",
    ]

    if memories:
        lines.extend(_format_memories(memories))
        lines.append("")

    if contexts:
        lines.extend(_format_contexts(contexts))
        lines.append("")

    lines.append(
        "Follow these conventions unless the user explicitly overrides them."
    )
    lines.append(_FOOTER)
    return "\n".join(lines)
