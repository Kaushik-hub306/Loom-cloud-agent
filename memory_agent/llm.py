"""
LLM wrapper — LiteLLM for multi-model routing.
Primary: Gemini Pro. Also supports Claude, GPT, DeepSeek.
"""
import os
import sys
from dataclasses import dataclass
from typing import Optional


# Model registry — swap with a config string
MODELS = {
    "gemini":    "gemini/gemini-2.5-pro",
    "claude":    "anthropic/claude-sonnet-4-6",
    "chatgpt":   "openai/gpt-4o",
    "deepseek":  "deepseek/deepseek-chat",
}

DEFAULT_MODEL = "gemini"


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: dict | None = None


class LLMRouter:
    """Thin LiteLLM wrapper. Swap model with a string."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("LOOM_LLM_MODEL", DEFAULT_MODEL)
        self._model_id = MODELS.get(self.model, self.model)

    async def ask(self, system_prompt: str, user_message: str,
                  temperature: float = 0.3) -> LLMResponse:
        """Send a message to the LLM. Returns structured response."""
        from litellm import acompletion

        try:
            response = await acompletion(
                model=self._model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=temperature,
                timeout=60,  # 60s total timeout
            )

            choice = response.choices[0]
            return LLMResponse(
                content=choice.message.content,
                model=getattr(response, 'model', self.model),
                usage={"total_tokens": getattr(response.usage, 'total_tokens', 0)} if response.usage else None,
            )

        except Exception as e:
            print(f"[llm] ERROR calling {self._model_id}: {e}", file=sys.stderr)
            raise


def format_memories_for_prompt(memories: list) -> str:
    """Format a list of Memory objects into a system prompt block."""
    if not memories:
        return ""

    lines = ["## Team Knowledge (from shared memory)", ""]
    by_domain = {}
    for m in memories:
        by_domain.setdefault(m.domain, []).append(m)

    for domain, mems in sorted(by_domain.items()):
        lines.append(f"### {domain.replace('_', ' ').title()}")
        for m in mems:
            confidence_label = "HIGH" if m.confidence >= 7 else "MED" if m.confidence >= 4 else "LOW"
            lines.append(f"- [{confidence_label}] {m.rule}")
            if m.example:
                lines.append(f"  Example: {m.example}")
        lines.append("")

    lines.append("Follow these conventions unless the user explicitly overrides them.")
    return "\n".join(lines)


def build_system_prompt(task: str, memories: list, role: str = "", thread_history: str = "",
                       contexts: list[dict] | None = None) -> str:
    """Build the full system prompt with memory context and conversation context injected."""
    memory_block = format_memories_for_prompt(memories)

    # Conversation context block — recent summaries from this or related channels
    context_block = ""
    if contexts:
        lines = ["## Recent Context (from shared memory)", ""]
        for ctx in contexts:
            domain = ctx.get("domain", "general")
            summary = ctx.get("summary", "")
            created = ctx.get("created_at", "")[:10] if ctx.get("created_at") else ""
            channel = ctx.get("channel", "")
            lines.append(f"- **{domain}** ({created}, #{channel}): {summary}")
        lines.append("")
        lines.append("Use this context to continue prior work. These are summaries of past conversations relevant to your current task.")
        context_block = "\n".join(lines) + "\n\n"

    history_block = ""
    if thread_history:
        history_block = f"""## Recent Conversation
{thread_history}

"""

    prompt = f"""You are a conversational AI assistant with access to your team's shared memory.
Your team has taught you conventions, preferences, and rules over time. You're a team member, not a tool — have a real conversation.

{memory_block}

{context_block}{history_block}## Current Task
{task}

### How to respond

1. If the request is ambiguous, ASK a clarifying question. Don't guess. Example: "Did you want JWT with refresh tokens or just short-lived access tokens?"

2. If what the user is asking contradicts a known convention, FLAG it. Example: "You taught me last week that Client X uses sentence case. You're asking for title case here — is this an exception or should I follow the existing rule?"

3. If you spot a pattern worth remembering, mention it. Example: "I've noticed you use this pattern across 3 projects now — want me to remember it as a team convention?"

4. Be concise but conversational. Ask questions when you need to. Push back when something doesn't match what the team knows. This is a dialogue, not a Q&A terminal.

5. Follow team conventions. If unsure about a convention, say so rather than guessing."""
    return prompt
