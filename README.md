# Loom Cloud Agent

Shared memory for AI coding tools. Teach once. Every AI on your team knows it.

## What it does

You teach a rule. Every AI tool your team uses — Claude Code, Codex, Cursor, Slack bot — follows it automatically.

```
You: /teach coding:convention Use async/await, never raw promises
 ──→ Supabase stores it
      ├──→ Claude Code loads it before writing any code
      ├──→ Codex loads it before running
      ├──→ Cursor loads it (via MCP)
      └──→ Slack bot knows it when answering questions
```

## Setup (zero to working in 10 min)

### Step 1: Supabase (free tier — 5 min)

1. Go to https://supabase.com → Sign up → **New Project**
2. Name: `loom-memory` | Password: generate one | Region: closest to you
3. Wait 2 min for it to provision
4. **Project Settings → Database → Connection String → URI tab** → copy it
5. Run this to set up the tables:

```bash
export LOOM_DB="postgresql://postgres:YOUR-PASSWORD@db.XXXXX.supabase.co:5432/postgres"

python3 -c "
import psycopg2
conn = psycopg2.connect('$LOOM_DB')
cur = conn.cursor()
cur.execute('CREATE EXTENSION IF NOT EXISTS vector;')
cur.execute('''
    CREATE TABLE IF NOT EXISTS rules (
        id TEXT PRIMARY KEY,
        domain TEXT NOT NULL DEFAULT 'general',
        rule_type TEXT NOT NULL DEFAULT 'convention',
        rule TEXT NOT NULL,
        example TEXT DEFAULT '',
        confidence INTEGER DEFAULT 5,
        sources JSONB DEFAULT '[]',
        source_type TEXT DEFAULT 'setup',
        embedding VECTOR(768),
        project TEXT DEFAULT 'loom-agent',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
''')
conn.commit()
print('Supabase ready. Tables created.')
conn.close()
"
```

### Step 2: Clone this repo

```bash
git clone https://github.com/Kaushik-hub306/Loom-cloud-agent.git
cd Loom-cloud-agent
pip install -r requirements.txt
```

### Step 3: Connect your AI tools

Pick any or all:

#### Claude Code

```bash
claude mcp add loom-memory \
  --env LOOM_DATABASE_URL="postgresql://postgres:YOUR-PASSWORD@db.XXXXX.supabase.co:5432/postgres" \
  -- python3 "$(pwd)/memory_agent/mcp_server.py"
```

Then open Claude Code and try:
```
use the session_init tool to load team conventions, then write a function that fetches a user
```

#### OpenAI Codex

```bash
codex mcp add loom-memory -- \
  --env LOOM_DATABASE_URL="postgresql://postgres:YOUR-PASSWORD@db.XXXXX.supabase.co:5432/postgres" \
  python3 "$(pwd)/memory_agent/mcp_server.py"
```

#### Cursor / Windsurf / any MCP-compatible editor

Add to your MCP config (`~/.cursor/mcp.json` or equivalent):

```json
{
  "mcpServers": {
    "loom-memory": {
      "command": "python3",
      "args": ["/path/to/Loom-cloud-agent/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://postgres:YOUR-PASSWORD@db.XXXXX.supabase.co:5432/postgres"
      }
    }
  }
}
```

#### Any HTTP client (curl, Postman, Python requests)

The memory agent API is at `/session_init`, `/ask`, `/teach`:

```bash
python3 -m uvicorn memory_agent.main:app --port 8000 &

# Load team context for a task
curl -X POST http://localhost:8000/session_init \
  -H 'Content-Type: application/json' \
  -d '{"task":"build auth middleware"}'

# Ask a question (calls your LLM with memory injected)
curl -X POST http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"task":"build auth","message":"should I use JWT or sessions?","model":"deepseek"}'

# Teach a rule
curl -X POST http://localhost:8000/teach \
  -H 'Content-Type: application/json' \
  -d '{"domain":"coding","rule_type":"convention","rule":"Use async/await for all I/O","confidence":8}'
```

### Step 4: Slack bot (optional — for non-technical team members)

1. Go to https://api.slack.com/apps → Create New App → From scratch
2. Name it, pick your workspace
3. **Socket Mode** → toggle ON
4. **OAuth & Permissions** → add scopes:
   - `app_mentions:read`
   - `chat:write`
   - `chat:write.public`
   - `commands`
   - `im:history`
   - `channels:history`
   - `channels:read`
5. Install to workspace → copy **Bot User OAuth Token** (`xoxb-...`)
6. **Basic Information → App-Level Tokens** → generate with `connections:write` → copy (`xapp-...`)
7. **Event Subscriptions** → toggle ON → subscribe to `app_mention`
8. **Slash Commands** → create `/teach` and `/stats`
9. Run:

```bash
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
export DEEPSEEK_API_KEY=sk-...
export LOOM_DATABASE_URL="postgresql://postgres:YOUR-PASSWORD@db.XXXXX.supabase.co:5432/postgres"
export LOOM_LLM_MODEL=deepseek
python3 task_agents/slack_bot.py
```

Invite the bot to a channel: `/invite @Loom`

## Deploy Slack bot to Railway (stay online 24/7)

1. Go to https://railway.com → Login with GitHub
2. New Project → Deploy from GitHub → select this repo
3. Add Variables:
   - `LOOM_DATABASE_URL` = your Supabase URI
   - `DEEPSEEK_API_KEY` = your key
   - `SLACK_BOT_TOKEN` = xoxb-...
   - `SLACK_APP_TOKEN` = xapp-...
   - `LOOM_LLM_MODEL` = deepseek
4. Deploy. Bot stays online even when your laptop is closed.

## Available LLM models

Set `LOOM_LLM_MODEL` to any of these:

| Model | String |
|-------|--------|
| DeepSeek | `deepseek` |
| Claude Sonnet | `claude` |
| GPT-4o | `chatgpt` |
| Gemini Pro | `gemini` |

Supported via LiteLLM. Add more from their 100+ model catalog.

## How memory works

### Three tools available to every AI

| Tool | What it does | When to use |
|------|-------------|-------------|
| `session_init` | Loads all relevant team conventions for a task | Start of every session |
| `recall_relevant` | Semantic search for specific conventions | Looking up a specific rule |
| `get_domain_rules` | Get all rules in a domain (coding, security, etc.) | Exploring a domain |

### Teaching flow

```
1. Teach (Slack, Claude Code, HTTP API)
   "Use Result<T, AppError> for all returns"
        │
2. Stored in Supabase
   domain: coding, confidence: 8, embedding: vector(768)
        │
3. Next session_init("build auth middleware")
   → pgvector cosine similarity search
   → Finds: "Use Result<T, AppError>" (semantically matches "middleware returns")
   → Injected into system prompt before LLM call
        │
4. AI follows the convention. Junior engineer's AI follows it too.
```

### Confidence system

- 1–3: Low — AI mentions it as optional
- 4–6: Medium — AI follows it unless user explicitly overrides
- 7–10: High — AI follows it strictly, flags violations

Duplicate teaches bump confidence automatically. Contradictory rules get flagged.

## Architecture

```
                    ┌──────────────────┐
                    │     Slack        │  (/teach, /stats, conversational)
                    │     (optional)   │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Memory Agent    │  FastAPI — /session_init, /ask, /teach
                    │  (FastAPI)       │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
       ┌──────▼──────┐ ┌────▼─────┐ ┌──────▼──────┐
       │   pgvector  │ │  Loom    │ │  LiteLLM    │
       │  (semantic) │ │ (domains)│ │ (models)    │
       └──────┬──────┘ └────┬─────┘ └─────────────┘
              │              │
              └──────┬───────┘
                     │
              ┌──────▼──────┐
              │  Supabase   │  Always online, shared across team
              │  Postgres   │
              └─────────────┘

                           ▲
       ┌───────────────────┼───────────────────┐
       │                   │                   │
  ┌────┴─────┐      ┌──────┴──────┐     ┌──────┴──────┐
  │ Claude   │      │   Codex     │     │  Cursor /   │
  │  Code    │      │             │     │  Windsurf   │
  └──────────┘      └─────────────┘     └─────────────┘
  All connect via MCP — same tools, same shared memory
```

## Tech stack

| Layer | Choice |
|-------|--------|
| Database | Supabase (managed Postgres) |
| Semantic search | pgvector (HNSW index, cosine similarity) |
| Embeddings | Gemini text-embedding-004 (768-dim, via LiteLLM) |
| LLM Router | LiteLLM (DeepSeek, Claude, GPT, Gemini) |
| Memory Agent | FastAPI (async, auto OpenAPI docs) |
| AI Tool Protocol | MCP (Model Context Protocol) — stdio JSON-RPC |
| Slack Bot | Slack Bolt (Socket Mode, async) |
| Deployment | Railway (auto-deploy from GitHub) |

## API reference

All endpoints return JSON.

### POST /session_init
Load team context before a task.
```json
Request:  { "task": "build auth middleware", "role": "engineer" }
Response: { "memories": [...], "context_prompt": "...", "memory_count": 3 }
```

### POST /ask
Full pipeline: load context → call LLM → return response.
```json
Request:  { "task": "...", "message": "...", "model": "deepseek", "thread_history": "" }
Response: { "response": "...", "model_used": "deepseek-v4-flash", "memories_used": 3 }
```

### POST /teach
Store a new convention.
```json
Request:  { "domain": "coding", "rule_type": "convention", "rule": "...", "confidence": 8 }
Response: { "id": "coding::convention::...", "confidence": 8 }
```

### GET /stats
Memory store statistics.
```json
Response: { "total_rules": 12, "domains": {"coding":5,"security":3}, "backend": "postgres" }
```

### GET /health
Health check.
```json
Response: { "status": "ok", "backend": "postgres" }
```
