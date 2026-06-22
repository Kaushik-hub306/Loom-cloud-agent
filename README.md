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

## Setup (step by step)

This takes about 10 minutes. The only thing you must have is a Postgres
database with `pgvector` — the easiest option is a free **Supabase** project
(used in the steps below). If you'd rather run Postgres locally, see
[Alternative: local Postgres with Docker](#alternative-local-postgres-with-docker).

### Step 0 — Requirements

- **Python 3.11 or newer.** Check with `python3 --version`.
- **A Supabase account** (free) — https://supabase.com — or any Postgres with
  the `pgvector` extension.
- **git**.

### Step 1 — Get the code

```bash
git clone https://github.com/Kaushik-hub306/Loom-cloud-agent.git loom
cd loom
```

> If the `loom` command or code seems to be missing after cloning, you may be on
> the wrong branch. Run `git checkout cursor/archive-legacy-prototype` (until it
> is merged into `main`).

### Step 2 — Create an isolated environment, then install

**Do not skip the virtual environment.** It keeps Loom's dependencies separate
and prevents clashes with any other tool on your machine.

```bash
python3 -m venv .venv          # create the environment (once)
source .venv/bin/activate      # activate it (every new terminal)
pip install -e ".[dev]"        # install Loom + its dependencies
```

After this, your shell prompt should start with `(.venv)`. Verify the install:

```bash
loom --version                 # -> loom, version 1.0.0
which loom                     # -> .../loom/.venv/bin/loom  (must be inside .venv)
```

> If `which loom` does **not** point inside `.venv`, your virtual environment is
> not active (or another program named `loom` is installed globally). Run
> `source .venv/bin/activate` again before continuing.

### Step 3 — Create a Supabase database

1. Go to https://supabase.com and create a new project. Remember the **database
   password** you set.
2. Wait for the project to finish provisioning (~1 minute).
3. Open **Project Settings → Database → Connection string** and copy the **URI**.
   It looks like this:

   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres
   ```

   Replace `[YOUR-PASSWORD]` with the database password from step 1.

You do **not** need to create any tables — Loom creates them automatically on
first run (including the `pgvector` extension).

### Step 4 — Configure your `.env`

Create your config file from the template:

```bash
cp .env.example .env
```

Then open `.env` in an editor and set **one required value** — your database URL:

```bash
LOOM_DATABASE_URL=postgresql://postgres:YOURPASSWORD@db.YOURREF.supabase.co:5432/postgres
```

Everything else is optional. For the simplest first run with no extra API keys,
also set these two (Loom will use plain text search instead of AI embeddings):

```bash
LOOM_LLM_PROVIDER=skip
LOOM_EMBEDDING_PROVIDER=none
```

> Prefer a guided setup? Run `loom init` instead of editing `.env` by hand. It
> asks for each value, validates the database connection before writing
> anything, and also generates an MCP config file for your AI tools.

### Step 5 — Verify it works

```bash
loom status
```

You should see a table with **Database: connected**. That confirms Loom reached
Supabase and created its tables. Add a few demo rules and start the API:

```bash
python scripts/seed_demo.py    # optional: inserts a handful of example rules
loom serve                     # starts the API at http://localhost:8000
```

In another terminal (remember to `source .venv/bin/activate` first):

```bash
curl http://localhost:8000/health     # -> {"status":"ok", ...}
loom recall "async database access"   # -> lists matching rules
```

That's it — Loom is running. Next, [connect it to your AI tools](#add-loom-to-your-ai-tools).

### Alternative: local Postgres with Docker

If you'd rather not use Supabase, run Postgres + pgvector locally with Docker and
point `LOOM_DATABASE_URL` at it:

```bash
docker run -d --name loom-pg \
  -e POSTGRES_PASSWORD=loom -e POSTGRES_DB=loom \
  -p 5432:5432 pgvector/pgvector:pg16
```

```bash
# in .env:
LOOM_DATABASE_URL=postgresql://postgres:loom@localhost:5432/loom
```

Then continue from [Step 5](#step-5--verify-it-works).

### Common setup problems

- **`ImportError: cannot import name 'cli'` or `loom` runs the wrong program.**
  Another package named `loom` is installed globally and is shadowing this one.
  Make sure your virtual environment is active (`source .venv/bin/activate`) and
  that `which loom` points inside `.venv`.
- **`Database: error` in `loom status`.** Double-check `LOOM_DATABASE_URL` in
  `.env` (password, project ref, no extra spaces). Confirm `.env` is in the
  folder you run `loom` from. Test the raw URL works from your machine.
- **Changes to `.env` seem ignored.** A real environment variable of the same
  name always overrides `.env`. Check with `printenv | grep LOOM_` and `unset`
  any stale values.

---

## Add Loom to your AI tools

`loom init` generates the config below. Embeddings are optional — without
`GEMINI_API_KEY`, Loom falls back to full-text search.

> **Important:** the `command` must be a Python that has Loom installed. If you
> used the virtual environment from setup, use its full path instead of bare
> `python3` — for example `/path/to/loom/.venv/bin/python`. Find it by running
> `which python` while the venv is active.

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
