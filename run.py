"""Railway entry point — Memory Agent API with Slack Bot."""
import os, sys, asyncio, threading, uvicorn

PORT = int(os.environ.get("PORT", "8000"))
print(f"[railway] Starting on port {PORT}...", flush=True)

# Slack bot in background
def start_slack():
    if not os.environ.get("SLACK_BOT_TOKEN") or not os.environ.get("SLACK_APP_TOKEN"):
        print("[railway] No Slack tokens — skipping bot", flush=True)
        return
    try:
        import nest_asyncio; nest_asyncio.apply()
    except ImportError:
        pass
    from task_agents.slack_bot import main as slack_main
    print("[railway] Slack bot running", flush=True)
    asyncio.run(slack_main())

threading.Thread(target=start_slack, daemon=True).start()

# FastAPI on main thread — Railway health checks /health
print(f"[railway] API → http://0.0.0.0:{PORT}/health", flush=True)
uvicorn.run("memory_agent.main:app", host="0.0.0.0", port=PORT, log_level="info")
