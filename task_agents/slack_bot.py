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

from memory_agent.memory import MemoryStore


MEMORY_AGENT_URL = os.environ.get("MEMORY_AGENT_URL", "http://localhost:8000")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
DEFAULT_MODEL = os.environ.get("LOOM_LLM_MODEL", "deepseek")

# ── Silent mode — Loom observes conversations without responding ──
# Set LOOM_SILENT=true to run as a silent listener. The bot reads all
# channel messages, captures context via the LLM gatekeeper, and NEVER
# responds. Cursor (or any other bot) handles all user interaction.
SILENT_MODE = os.environ.get("LOOM_SILENT", "").lower() in ("1", "true", "yes")

app = AsyncApp(token=SLACK_BOT_TOKEN)

# ── Debug middleware: log every incoming Slack event ───────
@app.middleware
async def debug_all_events(req, next):
    """Log all incoming events to stderr so we can trace what Slack sends."""
    body = req.body if hasattr(req, 'body') else {}
    if isinstance(body, dict):
        etype = body.get("event", {}).get("type", body.get("type", "unknown"))
        channel = body.get("event", {}).get("channel", "-")
        text = body.get("event", {}).get("text", "")[:50]
        print(f"[EVENT] type={etype} channel={channel} text={text}", file=sys.stderr, flush=True)
    return await next(req)

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

# ── Silent listener state ──────────────────────────────────
# Per-channel message buffer for conversation capture.
# Messages accumulate until a 3-minute lull triggers the gatekeeper.
_silent_buffer: dict[str, dict] = {}  # channel → {messages, last_msg_time, thread_ts}


async def _silent_capture(channel: str, thread_ts: str, user_msg: str,
                          bot_msg: str | None = None):
    """Buffer messages per channel. Gatekeeper fires after 3-min lull."""
    import time as _time
    now = _time.time()

    if channel not in _silent_buffer:
        _silent_buffer[channel] = {"messages": [], "last_msg_time": 0, "thread_ts": thread_ts}

    buf = _silent_buffer[channel]
    buf["messages"].append(f"[user]: {user_msg}")
    if bot_msg:
        buf["messages"].append(f"[assistant]: {bot_msg}")
    buf["last_msg_time"] = now
    buf["thread_ts"] = thread_ts

    # Keep only last 30 messages
    if len(buf["messages"]) > 30:
        buf["messages"] = buf["messages"][-30:]

    # Fire gatekeeper after 3-minute lull (async, don't block)
    async def _delayed_eval(capture_channel, capture_ts, eval_at):
        await asyncio.sleep(20)  # 20 seconds for testing
        buf_entry = _silent_buffer.get(capture_channel)
        if not buf_entry:
            print(f"[EVAL] no buffer entry for {capture_channel}", file=sys.stderr, flush=True)
            return
        print(f"[EVAL] last_msg={buf_entry['last_msg_time']} eval_at={eval_at} msgs={len(buf_entry['messages'])}", file=sys.stderr, flush=True)
        if buf_entry["last_msg_time"] <= eval_at:
            msgs = list(buf_entry["messages"])
            print(f"[EVAL] firing gatekeeper with {len(msgs)} messages", file=sys.stderr, flush=True)
            result = await evaluate_conversation_context(
                messages=msgs,
                channel=capture_channel,
                thread_ts=capture_ts,
            )
            print(f"[EVAL] result={result}", file=sys.stderr, flush=True)
            if capture_channel in _silent_buffer:
                _silent_buffer[capture_channel]["messages"] = []
        else:
            print(f"[EVAL] skipped — messages still arriving", file=sys.stderr, flush=True)

    asyncio.create_task(_delayed_eval(channel, thread_ts, now))

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

# ── Conversation context gatekeeper ────────────────────────

CONTEXT_PROMPT = """Review this conversation. Is there context worth remembering for future AI agents who will work on similar tasks? Look for:

- Decisions made and why
- Problems identified (bugs, security issues, design flaws)
- Workflows explained or demonstrated
- Context a future agent would need to continue this work
- Topic shifts — if the conversation changed to a completely new topic

If worth remembering as a NEW topic (different from earlier in the thread), respond:
CONTEXT_NEW: domain | one-sentence summary of what was discussed and decided

If worth remembering as a CONTINUATION of the same topic:
CONTEXT: domain | one-sentence summary

If nothing worth saving:
NOTHING

Examples:
CONTEXT: security | Audited auth module — found 3 issues: missing rate limiting, JWT not validated on refresh, session tokens in URL params
CONTEXT: architecture | Decided on repository pattern for DB access. Rejected Active Record because of testability concerns.
CONTEXT_NEW: deployment | Switched discussion to Railway deployment — resolved cold start issue by increasing min instances
NOTHING"""

# Debounce: track last gatekeeper evaluation per thread (3 min idle before re-eval)
_last_context_eval: dict[str, float] = {}


async def evaluate_conversation_context(messages: list[str], channel: str,
                                       thread_ts: str, workspace_id: str = "") -> dict | None:
    """Evaluate a conversation for context worth saving. Debounced: 3 min idle.

    Saves a raw blob backup first, then runs the LLM gatekeeper.
    Returns the saved context dict or None if NOTHING or LLM failure.
    """
    import time as _time

    # Debounce: skip if evaluated this thread in the last 3 minutes
    debounce_key = f"{channel}:{thread_ts}"
    now = _time.time()
    last = _last_context_eval.get(debounce_key, 0)
    if now - last < 180:  # 3 minutes
        return None
    _last_context_eval[debounce_key] = now

    conversation_text = "\n".join(messages[-20:])  # max 20 messages

    # 1. Blob backup — save raw messages first (data-loss guard)
    try:
        raw_messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": msg}
            for i, msg in enumerate(messages[-30:])
        ]
        store = MemoryStore()
        store.save_conversation_blob(
            channel=channel,
            thread_ts=thread_ts,
            messages=raw_messages,
            workspace_id=workspace_id,
        )
    except Exception:
        pass  # blob save is best-effort

    # 2. LLM gatekeeper
    try:
        from litellm import completion

        model = MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)
        response = completion(
            model=model,
            messages=[{
                "role": "user",
                "content": f"Conversation:\n{conversation_text}\n\n{CONTEXT_PROMPT}",
            }],
            temperature=0.1,
            max_tokens=200,
            timeout=15,  # fast timeout — context is background, not critical path
        )
        text = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[context] LLM gatekeeper failed: {e}", file=sys.stderr)
        return None  # blob already saved, no data loss

    # 3. Parse response
    is_new_topic = False
    if text.upper().startswith("CONTEXT_NEW:"):
        is_new_topic = True
        text = text[len("CONTEXT_NEW:"):].strip()
    elif text.upper().startswith("CONTEXT:"):
        text = text[len("CONTEXT:"):].strip()
    elif text.upper() == "NOTHING":
        return None
    else:
        return None  # unrecognized output — skip

    # Parse "domain | summary"
    if "|" in text:
        parts = text.split("|", 1)
        domain = parts[0].strip()
        summary = parts[1].strip()
    else:
        domain = "general"
        summary = text.strip()

    if not summary or len(summary) < 10:
        return None

    # 4. Save context summary
    try:
        store = MemoryStore()
        row_id = store.save_context_summary(
            channel=channel,
            thread_ts=thread_ts,
            summary=summary,
            domain=domain,
            message_count=len(messages),
            workspace_id=workspace_id,
            append=is_new_topic,
        )
        return {
            "id": row_id,
            "domain": domain,
            "summary": summary,
            "channel": channel,
            "thread_ts": thread_ts,
            "is_new_topic": is_new_topic,
        }
    except Exception as e:
        print(f"[context] save failed: {e}", file=sys.stderr)
        return None


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
    """Conversational bot with thread history + propose learning.
    In silent mode: only captures, never responds."""
    if SILENT_MODE:
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        text = event.get("text", "")
        if "<@" in text:
            text = text.split(">", 1)[1].strip() if ">" in text else text
        print(f"[MSG] mention channel={channel} text={text[:60]} silent=True", file=sys.stderr, flush=True)
        await _silent_capture(channel, thread_ts, user_msg=text)
        return

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

        # 5. Conversation context gatekeeper — fire-and-forget (doesn't block response)
        try:
            all_messages = (history.split("\n") if history else []) + [f"[user]: {user_message}"]
            asyncio.create_task(
                evaluate_conversation_context(
                    messages=all_messages,
                    channel=channel,
                    thread_ts=reply_ts,
                )
            )
        except Exception:
            pass

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
async def handle_all_messages(event, say, client):
    """Handle ALL messages: DMs, channel messages, silent capture."""
    channel = event.get("channel", "")
    ts = event.get("ts", "")
    text = event.get("text", "").strip()
    is_dm = event.get("channel_type") == "im"
    is_bot = bool(event.get("bot_id"))

    print(f"[MSG] channel={channel} is_dm={is_dm} is_bot={is_bot} text={text[:60]} silent={SILENT_MODE}", file=sys.stderr, flush=True)
    # ── Silent mode: capture everything, never respond ──
    if SILENT_MODE:
        if text:
            user_msg = text if not is_bot else ""
            bot_msg = text if is_bot else ""
            await _silent_capture(channel, ts, user_msg=user_msg, bot_msg=bot_msg)
        return

    # ── Interactive mode ──
    if not is_dm:
        return  # only respond to DMs in interactive mode

    user_message = text
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

        # Conversation context gatekeeper — fire-and-forget
        try:
            all_messages = (history.split("\n") if history else []) + [f"[user]: {user_message}"]
            asyncio.create_task(
                evaluate_conversation_context(
                    messages=all_messages,
                    channel=channel,
                    thread_ts=thread_ts,
                )
            )
        except Exception:
            pass

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

    mode = "SILENT — capturing all messages, never responding" if SILENT_MODE else "INTERACTIVE — responding to @mentions and DMs"
    print(f"[slack] Starting → memory agent at {MEMORY_AGENT_URL}")
    print(f"[slack] Mode: {mode}")
    print(f"[slack] Model: {DEFAULT_MODEL} | Features: thread history + context gatekeeper")
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
