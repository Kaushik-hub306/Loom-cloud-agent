"""Railway entry point — Memory Agent API + Slack Bot + MCP Server."""
import os, sys, asyncio, threading, uvicorn

PORT = int(os.environ.get("PORT", "8000"))
SILENT = os.environ.get("LOOM_SILENT", "").lower() in ("1", "true", "yes")

print(f"[railway] Starting on port {PORT}...", flush=True)
print(f"[railway] Silent mode: {'ON — capturing all messages, never responding' if SILENT else 'OFF — responding to @mentions and DMs'}", flush=True)

# Slack bot in background (silent listener or interactive, depending on LOOM_SILENT)
def start_slack():
    if not os.environ.get("SLACK_BOT_TOKEN") or not os.environ.get("SLACK_APP_TOKEN"):
        print("[railway] No Slack tokens — skipping bot", flush=True)
        return
    try:
        import nest_asyncio; nest_asyncio.apply()
    except ImportError:
        pass
    from task_agents.slack_bot import main as slack_main
    mode = "silent listener" if SILENT else "interactive"
    print(f"[railway] Slack bot running ({mode})", flush=True)
    asyncio.run(slack_main())

threading.Thread(target=start_slack, daemon=True).start()

# FastAPI on main thread — Railway health checks /health + session_init endpoint
print(f"[railway] API → http://0.0.0.0:{PORT}/health", flush=True)
print(f"[railway] Endpoints: /session_init /ask /teach /stats /health", flush=True)
uvicorn.run("memory_agent.main:app", host="0.0.0.0", port=PORT, log_level="info")
