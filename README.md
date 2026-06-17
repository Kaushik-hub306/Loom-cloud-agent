# Loom Cloud Agent

Shared memory for AI tools. Teach once. Every AI on your team remembers.

## Quick start

Paste this into Claude Code. It does the rest.

```
Set up Loom Cloud Agent for me.

1. Ask me for my Supabase connection URI (if I don't have one, tell me to create a free project at supabase.com → Project Settings → Database → Connection String → URI tab).

2. Run:
git clone https://github.com/Kaushik-hub306/Loom-cloud-agent.git
cd Loom-cloud-agent
pip install -r requirements.txt

3. Set up the database using the URI I gave you (create vector extension + rules table).

4. Run: claude mcp add loom-memory --env LOOM_DATABASE_URL="MY_URI" -- python3 "$(pwd)/memory_agent/mcp_server.py"

5. Call session_init to verify it loads. Tell me when I'm live.
```

## How it works

```
Teach a rule      →    Supabase    →    Every AI follows it
/teach coding:      (shared DB)       Claude Code, Codex,
convention "use                       Cursor, Slack bot
async/await"
```

## Supported AI tools

Claude Code, Codex, Cursor, Windsurf, any MCP editor, Slack bot.

## Supported LLMs

DeepSeek, Claude, GPT-4o, Gemini. Swap with one string. Via LiteLLM.

## What people store

| Domain | Example rule |
|--------|-------------|
| coding | Use Pydantic models, never raw dicts |
| architecture | Repository pattern for all DB access |
| security | JWTs expire in 15 min, refresh tokens |
| testing | 85% coverage minimum |
| process | 1 review per PR, 2 for auth |

## Deploy (optional)

Railway free tier keeps the Slack bot online 24/7. Add the env vars and deploy from GitHub.

## Tech

Supabase + pgvector | FastAPI | LiteLLM | MCP | Slack Bolt
