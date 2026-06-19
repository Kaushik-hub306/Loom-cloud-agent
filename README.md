# Loom

**Loom is a shared memory layer for AI coding agents.** It lets tools like Claude
Code, Claude Desktop, and Cursor start every session with persistent team
knowledge: coding conventions, architectural decisions, security policies, and
short summaries of recent engineering conversations.

Without Loom, each AI coding session starts from zero. With Loom, your agent
calls `session_init` at the start of a session and receives a clean
system-prompt block of durable team memory before it writes a single line of
code.

```
<!-- LOOM:SESSION_CONTEXT -->
## Team knowledge (3 rules | 1 past conversations)

### Coding
- [HIGH] convention: Use async/await for all I/O operations.
  Example: Use asyncpg instead of blocking psycopg2 in FastAPI routes.

### Recent conversations
- [architecture] (2026-06-19, #eng): We chose to keep the Slack worker decoupled from the DB.

Follow these conventions unless the user explicitly overrides them.
<!-- /LOOM:SESSION_CONTEXT -->
```

---

## How it works

Loom runs as up to three independent processes:

| Process | Command | Role |
|---|---|---|
| FastAPI memory service | `loom serve` | HTTP API for teach/recall/session_init/ask + internal Slack writes |
| MCP stdio server | `python -m loom.mcp.server` | Exposes Loom tools to Claude Code / Cursor / Claude Desktop |
| Slack worker | `loom slack` | Silently captures useful engineering context from Slack |

Key boundaries (enforced by tests):

- All database access goes through `loom/db.py` and `loom/memory/store.py`.
- Only `loom/config.py` reads environment variables.
- The Slack worker never touches the database; it talks to FastAPI over HTTP.
- The MCP server never imports FastAPI or Slack and runs with only `LOOM_DATABASE_URL`.
- MCP writes JSON-RPC to stdout only; all logs go to stderr.

---

## 10-minute local setup

### 1. Requirements

- Python 3.11+
- A PostgreSQL database with the [`pgvector`](https://github.com/pgvector/pgvector)
  extension (Supabase works out of the box; locally you can use the
  `pgvector/pgvector` Docker image).

### 2. Install

```bash
git clone <your-repo-url> loom && cd loom
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Run a local Postgres with pgvector (optional)

```bash
docker run -d --name loom-pg \
  -e POSTGRES_PASSWORD=loom -e POSTGRES_DB=loom \
  -p 5432:5432 pgvector/pgvector:pg16
```

### 4. Configure

```bash
cp .env.example .env
# Edit .env: set LOOM_DATABASE_URL (required). Everything else is optional.
```

Or run the guided wizard, which validates credentials before writing anything:

```bash
loom init
```

`loom init` writes:

- `.env` with your settings (secrets never echoed),
- a ready-to-copy MCP config at `~/.claude/loom-mcp-config.json` (contains only
  `LOOM_DATABASE_URL` and, if configured, `GEMINI_API_KEY` — no Slack secrets).

It refuses to write any files if credential validation fails.

### 5. Verify

```bash
loom serve                 # starts the API; visit http://localhost:8000/health
loom status                # status table for DB, API, Slack, and counts
```

---

## Supabase + pgvector

1. Create a Supabase project.
2. In the SQL editor, enable pgvector: `create extension if not exists vector;`
   (Loom's migrations also do this automatically.)
3. Copy the connection string (pooler or direct) into `LOOM_DATABASE_URL`.

The schema in `supabase/schema.sql` is idempotent and is applied automatically
on startup. You can also apply it manually.

---

## Add Loom to your AI tools

`loom init` generates the config below. Embeddings are optional — without
`GEMINI_API_KEY`, Loom falls back to full-text search.

### Claude Code

```bash
claude mcp add loom-memory -- python3 -m loom.mcp.server
```

### Cursor

Create `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "loom": {
      "command": "python3",
      "args": ["-m", "loom.mcp.server"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://...",
        "GEMINI_API_KEY": "..."
      }
    }
  }
}
```

### Claude Desktop

Add the same `mcpServers` block to your Claude Desktop config.

---

## Teaching and recalling

```bash
loom teach "coding" "convention" "Use async/await for all I/O operations" \
  --example "Use asyncpg in FastAPI routes." --confidence 8

loom recall "async database access"
```

Teaching the same rule again bumps its confidence instead of duplicating it.

### API examples

```bash
# Health (always unauthenticated)
curl http://localhost:8000/health

# Teach
curl -X POST http://localhost:8000/teach \
  -H 'Content-Type: application/json' \
  -H 'X-Loom-Api-Key: <key if configured>' \
  -d '{"domain":"coding","rule_type":"convention","rule":"Use async/await for I/O."}'

# Session init (read-only; returns a prompt block)
curl -X POST http://localhost:8000/session_init \
  -H 'Content-Type: application/json' \
  -d '{"task":"Refactor a FastAPI route that queries Postgres","channel":"general"}'
```

Public routes: `GET /health`, `GET /stats`, `GET /export`, `POST /import`,
`POST /session_init`, `POST /teach`, `POST /recall`, `POST /ask`.
Internal (Slack-only) routes: `POST /internal/conversation_blob`,
`POST /internal/context_summary`.

---

## Backup: export / import

```bash
loom export > backup.json
loom export --include-embeddings > backup_with_vectors.json
loom import backup.json                       # regenerates embeddings by default
loom import backup.json --no-regenerate-embeddings
```

---

## Slack setup (optional)

Loom's Slack app runs in **silent observer mode** by default: it does not post
channel replies, but it must still be invited to channels it should read.

Bot token scopes:

```
channels:history channels:read app_mentions:read chat:write im:history commands
```

Optional scopes if needed: `groups:history groups:read mpim:history users:read`.

Socket Mode: create an App-Level Token with `connections:write`.

Event subscriptions: `message.channels`, `app_mention`, `message.im`.

Slash commands: `/loom-teach`, `/loom-stats`, `/loom-recall`.

Run the worker (it waits for the API `/health` before connecting):

```bash
loom slack                 # silent observer (default)
loom slack --interactive   # also answers DMs/mentions via /ask
```

---

## Running everything locally

```bash
loom serve                       # API service
loom slack                       # Slack worker (separate process)
python -m loom.mcp.server        # MCP server (usually launched by your AI tool)
python scripts/seed_demo.py      # optional: seed a few demo memories
```

---

## Railway deployment

`Procfile` and `railway.toml` define two services from one repo:

1. `loom-api` runs `loom serve` and exposes `/health`.
2. `loom-slack` runs `loom slack` (no external HTTP port needed).
3. Set `LOOM_API_BASE_URL` in `loom-slack` to the `loom-api` internal/private URL
   (or its public URL).
4. Both services need DB, LLM, embedding, `LOOM_API_KEY`, and
   `LOOM_INTERNAL_API_TOKEN`. The Slack worker also needs the Slack bot and app
   tokens.

In production (`LOOM_ENV=production`), startup fails if `LOOM_API_KEY` or
`LOOM_INTERNAL_API_TOKEN` are missing.

---

## Testing

```bash
loom test                        # runs pytest
loom test -- tests/test_config.py -q
```

Unit tests run without any external credentials. Database integration tests are
skipped cleanly unless `TEST_DATABASE_URL` is set:

```bash
export TEST_DATABASE_URL=postgresql://postgres:loom@localhost:5432/loom_test
loom test
```

---

## Troubleshooting

- **Missing pgvector**: ensure the database supports `CREATE EXTENSION vector`.
  Supabase and the `pgvector/pgvector` image both include it.
- **Slack Socket Mode token wrong**: the app token must start with `xapp-` and
  have `connections:write`; the bot token starts with `xoxb-`.
- **API auth 401**: when `LOOM_API_KEY` is set, send `X-Loom-Api-Key` on all
  non-`/health` public routes; internal routes need `X-Loom-Internal-Token`.
- **No semantic search**: if `GEMINI_API_KEY` is missing, Loom automatically
  falls back to full-text search (a degraded but valid mode).
- **LLM disabled**: with `LOOM_LLM_PROVIDER=skip`, `/ask`, interactive Slack
  replies, and the Slack gatekeeper are disabled and return clear messages.
- **MCP debugging**: the MCP server writes only JSON-RPC to stdout; check
  **stderr** for startup and error logs.

## Security notes

- Do not expose the API publicly without setting `LOOM_API_KEY`.
- The Slack bot can read the channels it is invited to.
- Conversation blobs and context summaries expire automatically
  (`LOOM_BLOB_TTL_DAYS`, `LOOM_CONTEXT_TTL_DAYS`).
- Secrets are never logged; only redacted forms are emitted.

---

## License

MIT
