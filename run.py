"""Railway entry point — runs Memory Agent API + Slack Bot together."""
import os
import sys
import asyncio
import threading
import uvicorn

print("[railway] Starting Loom Cloud Agent...")

# Start FastAPI in a background thread
def start_api():
    port = int(os.environ.get("PORT", "8000"))
    print(f"[railway] Memory Agent API on port {port}")
    uvicorn.run("memory_agent.main:app", host="0.0.0.0", port=port, log_level="info")

# Start Slack bot in another thread (or skip if no tokens)
def start_slack():
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not slack_token or not app_token:
        print("[railway] Slack tokens not set — skipping bot")
        return

    from task_agents.slack_bot import main as slack_main
    print("[railway] Starting Slack bot...")
    asyncio.run(slack_main())

api_thread = threading.Thread(target=start_api, daemon=True)
api_thread.start()

start_slack()  # blocks here (Socket Mode keeps it alive)
