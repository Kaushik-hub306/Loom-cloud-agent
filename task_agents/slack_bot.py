"""
Slack bot — conversational memory agent.
- Thread history: reads past messages in the thread for multi-turn context
- Propose learning: after each response, asks "noticed anything worth remembering?"
- Approve/Dismiss buttons for proposed learnings
"""
import os
import sys
import re
import asyncio
import httpx
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler


MEMORY_AGENT_URL = os.environ.get("MEMORY_AGENT_URL", "http://localhost:8000")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
DEFAULT_MODEL = os.environ.get("LOOM_LLM_MODEL", "deepseek")

app = AsyncApp(token=SLACK_BOT_TOKEN)

# ── Memory Agent client ────────────────────────────────────

class MemoryAgentClient:
    def __init__(self, base_url: str = MEMORY_AGENT_URL):
        self.base_url = base_url
        self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        return self._client

    async def ask(self, task: str, message: str, role: str = "",
                  model: str = DEFAULT_MODEL, thread_history: str = "") -> dict:
        client = await self._get_client()
        r = await client.post(f"{self.base_url}/ask", json={
            "task": task, "message": message, "role": role, "model": model,
            "thread_history": thread_history,
        })
        r.raise_for_status()
        return r.json()

    async def teach(self, domain: str, rule: str, rule_type: str = "convention",
                    example: str = "", confidence: int = 7) -> dict:
        client = await self._get_client()
        r = await client.post(f"{self.base_url}/teach", json={
            "domain": domain, "rule_type": rule_type, "rule": rule,
            "example": example, "confidence": confidence,
        })
        r.raise_for_status()
        return r.json()

    async def stats(self) -> dict:
        client = await self._get_client()
        r = await client.get(f"{self.base_url}/stats")
        r.raise_for_status()
        return r.json()


agent = MemoryAgentClient()

# ── Thread history ─────────────────────────────────────────

async def get_thread_history(client, channel: str, thread_ts: str, max_messages: int = 10) -> str:
    """Fetch recent messages from a Slack thread to include as conversation context."""
    try:
        result = await client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=max_messages + 1,  # +1 for the root message
        )
        messages = result.get("messages", [])
        # Skip the first (root) message, take the rest
        history = []
        for msg in messages[1:][-max_messages:]:
            text = msg.get("text", "")
            # Strip bot mentions
            text = re.sub(r'<@[^>]+>', '', text).strip()
            if text:
                role = "assistant" if msg.get("bot_id") else "user"
                history.append(f"[{role}]: {text}")
        return "\n".join(history) if history else ""
    except Exception as e:
        print(f"[slack] thread history error: {e}", file=sys.stderr)
        return ""


# ── Propose learning ───────────────────────────────────────

PROPOSE_PROMPT = """Based on the conversation above, did the user share any preferences, conventions, rules, or patterns worth remembering for future conversations? Look for things like:

- "We always do X this way"
- "I prefer Y over Z"
- "Never use W for this"
- Any correction or redirection of your response

If you found something worth remembering, respond with exactly this format (one per finding):

PROPOSE: domain name here | rule_type_here | The specific rule text

If you found nothing, respond with exactly: NOTHING

Examples:
PROPOSE: coding | convention | Use async/await for all database queries
PROPOSE: brand | style | Social posts use sentence case, never title case
NOTHING"""


async def propose_learnings(conversation: str, user_message: str):
    """Ask the LLM to extract learnings from the conversation."""
    try:
        from litellm import completion

        model = MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)
        response = completion(
            model=model,
            messages=[
                {"role": "user", "content": f"""
Previous conversation:
{conversation}

User's latest message: {user_message}

{PROPOSE_PROMPT}"""},
            ],
            temperature=0.1,
            max_tokens=300,
        )
        text = response.choices[0].message.content.strip()
        return [line for line in text.split("\n") if line.upper().startswith("PROPOSE:")]
    except Exception as e:
        print(f"[slack] propose error: {e}", file=sys.stderr)
        return []


def parse_proposal(line: str) -> dict | None:
    """Parse 'PROPOSE: domain | rule_type | rule text' into a dict."""
    line = re.sub(r'^PROPOSE:\s*', '', line)
    parts = [p.strip() for p in line.split("|", 2)]
    if len(parts) >= 3:
        return {"domain": parts[0], "rule_type": parts[1], "rule": parts[2]}
    return None


# ── Model registry ─────────────────────────────────────────

MODELS = {
    "gemini":   "gemini/gemini-2.5-pro",
    "deepseek": "deepseek/deepseek-chat",
    "claude":   "anthropic/claude-sonnet-4-6",
    "chatgpt":  "openai/gpt-4o",
}

# ── Slack handlers ─────────────────────────────────────────

@app.event("app_mention")
async def handle_mention(event, say, client):
    """Conversational bot with thread history + propose learning."""
    user_message = event.get("text", "")
    channel = event.get("channel", "")
    ts = event.get("ts", "")
    # Only use thread if user is already in a thread (not a new channel message)
    in_thread = event.get("thread_ts") and event["thread_ts"] != ts

    # Strip bot mention
    if "<@" in user_message:
        parts = user_message.split(">", 1)
        user_message = parts[1].strip() if len(parts) > 1 else user_message

    if not user_message:
        await say("What can I help with?")
        return

    # Reply in channel (not thread) unless already in a thread
    ack = await say("…", thread_ts=event["thread_ts"]) if in_thread else await say("…")

    reply_ts = event["thread_ts"] if in_thread else ts

    try:
        # 1. Get thread history (only if in a thread)
        history = await get_thread_history(client, channel, reply_ts) if in_thread else ""
        full_message = f"{history}\n[user]: {user_message}" if history else user_message

        # 2. Call memory agent (context + thread history automatically injected)
        result = await agent.ask(
            task=user_message[:200],
            message=full_message,
            role="team_member",
            thread_history=history,
        )

        response_text = result["response"]
        memories_used = result.get("memories_used", 0)
        model_used = result.get("model_used", "unknown")

        # 3. Update the "…" message with response
        footer = f"\n\n_— {model_used}"
        if memories_used > 0:
            footer += f", {memories_used} memories used"
        footer += "_"
        await client.chat_update(
            channel=channel,
            ts=ack["ts"],
            text=response_text[:2900] + footer,
        )

        # 4. Propose learnings (as channel reply, not thread)
        proposals = await propose_learnings(history, user_message)

        for i, line in enumerate(proposals):
            proposal = parse_proposal(line)
            if not proposal:
                continue

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"💡 *Should I remember this?*\n> _{proposal['rule_type']}_ in `{proposal['domain']}`: {proposal['rule']}"
                    }
                },
                {
                    "type": "actions",
                    "block_id": f"learn_{i}_{ts}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✓ Remember"},
                            "style": "primary",
                            "action_id": "approve_learning",
                            "value": f"{proposal['domain']}|{proposal['rule_type']}|{proposal['rule']}"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✗ Dismiss"},
                            "style": "danger",
                            "action_id": "dismiss_learning",
                            "value": "dismiss"
                        }
                    ]
                }
            ]
            await client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text="Should I remember this?",
            )

    except Exception as e:
        print(f"[slack] error: {e}", file=sys.stderr)
        await client.chat_update(
            channel=channel,
            ts=ack["ts"],
            text=f"Sorry, something went wrong. Try again?\n\n`{str(e)[:200]}`",
        )


@app.action("approve_learning")
async def handle_approve(ack, body, client, respond):
    await ack()
    value = body["actions"][0]["value"]
    domain, rule_type, rule = [v.strip() for v in value.split("|", 2)]

    try:
        result = await agent.teach(domain=domain, rule_type=rule_type, rule=rule)
        await respond(f"✓ Remembered! `{domain}/{rule_type}` — confidence {result['confidence']}/10")
    except Exception as e:
        await respond(f"Failed to store: {e}")


@app.action("dismiss_learning")
async def handle_dismiss(ack, body, client):
    await ack()
    # Remove the blocks to clean up
    try:
        await client.chat_delete(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
        )
    except Exception:
        pass  # Message might already be gone


@app.event("message")
async def handle_dm(event, say, client):
    """Handle DMs conversationally."""
    if event.get("channel_type") != "im":
        return

    user_message = event.get("text", "")
    channel = event.get("channel", "")
    thread_ts = event.get("ts", "")

    if not user_message.strip():
        return

    ack = await say("…")

    try:
        history = await get_thread_history(client, channel, thread_ts)
        full_message = f"{history}\n[user]: {user_message}" if history else user_message

        result = await agent.ask(task=user_message[:200], message=full_message, thread_history=history)
        await client.chat_update(
            channel=channel,
            ts=ack["ts"],
            text=result["response"][:2900],
        )

        proposals = await propose_learnings(history, user_message)
        for i, line in enumerate(proposals):
            proposal = parse_proposal(line)
            if not proposal:
                continue
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"💡 *Should I remember this?*\n> _{proposal['rule_type']}_ in `{proposal['domain']}`: {proposal['rule']}"
                    }
                },
                {
                    "type": "actions",
                    "block_id": f"dm_learn_{i}_{thread_ts}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✓ Remember"},
                            "style": "primary",
                            "action_id": "approve_learning",
                            "value": f"{proposal['domain']}|{proposal['rule_type']}|{proposal['rule']}"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✗ Dismiss"},
                            "style": "danger",
                            "action_id": "dismiss_learning",
                            "value": "dismiss"
                        }
                    ]
                }
            ]
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text="Should I remember this?"
            )

    except Exception as e:
        await client.chat_update(
            channel=channel,
            ts=ack["ts"],
            text=f"Error: {e}",
        )


@app.command("/teach")
async def handle_teach(ack, command, respond):
    await ack()
    text = command.get("text", "")
    if ":" not in text:
        await respond("Usage: `/teach domain:type The rule text`\nExample: `/teach coding:convention Use async/await, never raw promises`")
        return

    header, _, rule = text.partition(" ")
    if ":" in header:
        domain, _, rule_type = header.partition(":")
    else:
        domain, rule_type = header, "convention"

    if not rule.strip():
        await respond("Please include the rule text.")
        return

    try:
        result = await agent.teach(domain=domain.strip(), rule_type=rule_type.strip(), rule=rule.strip())
        await respond(f"✓ Taught! `{domain}/{rule_type}` — confidence {result['confidence']}/10")
    except Exception as e:
        await respond(f"Failed: {e}")


@app.command("/stats")
async def handle_stats(ack, command, respond):
    await ack()
    try:
        s = await agent.stats()
        domains = "\n".join(f"• {d}: {c} rules" for d, c in sorted(s["domains"].items()))
        await respond(f"*Memory Store* ({s['backend']})\n{s['total_rules']} total rules\n\n{domains}")
    except Exception as e:
        await respond(f"Error: {e}")


async def main():
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        print("[slack] Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN to start", file=sys.stderr)
        print("[slack] Run: export SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-...", file=sys.stderr)
        sys.exit(1)

    print(f"[slack] Starting → memory agent at {MEMORY_AGENT_URL}")
    print(f"[slack] Model: {DEFAULT_MODEL} | Features: thread history + propose learning")
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
