"""Railway entry point — Slack Bot (main thread) + FastAPI (background)."""
import os, sys, asyncio, threading, uvicorn

PORT = int(os.environ.get("PORT", "8000"))
SILENT = os.environ.get("LOOM_SILENT", "").lower() in ("1", "true", "yes")

print(f"[railway] Starting on port {PORT}...", flush=True)
print(f"[railway] Silent mode: {'ON — capturing all messages, never responding' if SILENT else 'OFF — responding to @mentions and DMs'}", flush=True)

# FastAPI in background thread — health checks + API
def start_api():
    print(f"[railway] API → http://0.0.0.0:{PORT}/health", flush=True)
    uvicorn.run("memory_agent.main:app", host="0.0.0.0", port=PORT, log_level="info")

threading.Thread(target=start_api, daemon=True).start()

# Slack bot on MAIN thread — the event loop that actually receives events
if not os.environ.get("SLACK_BOT_TOKEN") or not os.environ.get("SLACK_APP_TOKEN"):
    print("[railway] No Slack tokens — skipping bot", flush=True)
else:
    from task_agents.slack_bot import main as slack_main
    mode = "silent listener" if SILENT else "interactive"
    print(f"[railway] Slack bot running ({mode})", flush=True)
    asyncio.run(slack_main())
