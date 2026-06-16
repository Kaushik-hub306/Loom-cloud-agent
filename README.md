# Loom Cloud Agent

Shared memory for AI coding agents. Teach your AI once — every team member's AI benefits.

## How it works

1. Teach rules in Slack: `/teach coding:convention Use Pydantic models, never raw dicts`
2. Open Claude Code. It automatically loads team conventions.
3. Claude writes code that follows your team's rules. Every time.

## Setup (5 minutes)

### 1. Clone
```bash
git clone https://github.com/Kaushik-hub306/Loom-cloud-agent.git
cd Loom-cloud-agent
pip install -r requirements.txt
```

### 2. Connect Claude Code
```bash
claude mcp add loom-memory \
  --env LOOM_DATABASE_URL="your-supabase-url" \
  -- python3 "$(pwd)/memory_agent/mcp_server.py"
```

Get the Supabase URL from your team lead.

### 3. Test it
Open Claude Code and type:
```
use the session_init tool to load team conventions, then write a function that fetches a user
```

### 4. Slack bot (optional)
```bash
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
export DEEPSEEK_API_KEY=sk-...
export LOOM_DATABASE_URL=your-supabase-url
python3 task_agents/slack_bot.py
```

## Architecture

```
Slack (/teach) ──→ Supabase (shared DB) ←── Claude Code (MCP)
                                         ←── Codex (MCP)
                                         ←── Any MCP-compatible AI
```

## What it stores

| Domain | Example |
|--------|---------|
| coding | Use Result<T, AppError> for all returns |
| architecture | New services use FastAPI + PostgreSQL |
| security | JWTs expire in 15 min, use refresh tokens |
| testing | 85% coverage minimum on all new endpoints |
| process | 1 review required, 2 for auth/billing |

## Tech

- Supabase + pgvector for semantic shared memory
- FastAPI for the memory agent API
- Slack Bolt for conversational teach/propose loop
- MCP (Model Context Protocol) for Claude Code integration
- LiteLLM for multi-model support
