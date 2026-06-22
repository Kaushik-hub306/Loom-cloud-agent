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

## Quick start (one command)

Clone, run the setup script, answer the prompts, and **paste the MCP config it
prints into your coding tool**. That's the whole thing.

```bash
git clone https://github.com/Kaushik-hub306/Loom-cloud-agent.git loom
cd loom
bash setup.sh
```

`setup.sh` will:

1. Create an isolated environment and install Loom (no clashes with anything
   else on your machine).
2. Ask you for everything it needs, right in the terminal:
   - **Database URL** (required) — your Supabase connection string
     (Project Settings → Database → Connection string → URI).
   - **Gemini API key** (optional) — enables smart semantic search; Enter to skip.
   - **Slack tokens** (optional) — Enter to skip.
3. Verify the database connection and create the tables.
4. Print a ready-to-paste **MCP config** (and save it to `loom-mcp.json`).

Finally, paste that JSON into your coding tool and restart it:

- **Cursor** → create `.cursor/mcp.json` in your project and paste it in.
- **Claude Code** → `claude mcp add-json loom '<the JSON>'`.
- **Claude Desktop** → add the block to `claude_desktop_config.json`.

Your agent can now call Loom's tools (`session_init`, `recall`, `teach`,
`get_stats`). The generated config uses an absolute path to the project's Python,
so it works no matter which folder your tool launches it from.

> Re-run `bash setup.sh` any time — it's safe and will offer to reuse your
> existing `.env`. Prefer to do it by hand? See the manual steps below.

---

## Setup (step by step, manual)

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

## Slack setup (optional, step by step) — beta

Slack is optional — Loom works great as MCP-only. Add Slack when you want Loom to
**silently learn from team conversations** automatically.

### What the Slack bot does (and doesn't do)

- **Silent observer (default).** Once invited to a channel, it reads every
  message but **never posts in the channel**. It buffers each conversation, and
  after the thread goes idle (~3 minutes) a **gatekeeper LLM decides whether the
  discussion is worth remembering**. If yes, a short summary is saved to your
  database; if not, it's discarded. (This gatekeeper is the chat LLM — DeepSeek,
  OpenAI, Claude, or Gemini. Without an LLM key, capture is disabled.)
- **It does not auto-reply to messages.** The bot only "speaks" when you
  explicitly ask it: via a slash command (private reply), or — in
  `--interactive` mode — when you `@mention` it or DM it.
- **Retrieving memory** is on demand: `/loom-recall`, an `@mention` (interactive
  mode), or your coding agent calling `session_init`.

### Step 1 — Create the Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**.
2. Name it (e.g. "Loom") and pick your workspace.

### Step 2 — Enable Socket Mode (no public URL needed)

1. In the app, open **Settings → Socket Mode** and toggle it **on**.
2. When prompted, create an **App-Level Token** with the scope
   **`connections:write`**. Copy it — it starts with **`xapp-`**.
   (This is your `LOOM_SLACK_APP_TOKEN`.)

### Step 3 — Add bot token scopes

Open **Features → OAuth & Permissions → Scopes → Bot Token Scopes** and add:

```
channels:history     # read messages in public channels it's invited to
channels:read        # see channel metadata
app_mentions:read    # receive @mentions
chat:write           # post replies (used only by slash commands / interactive mode)
im:history           # read DMs sent to the bot
commands             # enable slash commands
```

Optional (only if you need them): `groups:history`, `groups:read` (private
channels), `mpim:history` (group DMs), `users:read` (resolve usernames).

### Step 4 — Subscribe to events

Open **Features → Event Subscriptions**, toggle **on**, and under
**Subscribe to bot events** add:

```
message.channels     # messages in public channels
app_mention          # when someone @mentions the bot
message.im           # direct messages to the bot
```

### Step 5 — Create slash commands

Open **Features → Slash Commands → Create New Command** and add these three
(the "Request URL" can be any placeholder like `https://example.com` — Socket
Mode delivers them, so the URL is not actually called):

```
/loom-teach    Teach Loom a durable team rule
/loom-recall   Search Loom memory
/loom-stats    Show Loom memory stats
```

### Step 6 — Install and copy the bot token

1. Open **Settings → Install App → Install to Workspace** and authorize.
2. Copy the **Bot User OAuth Token** — it starts with **`xoxb-`**.
   (This is your `LOOM_SLACK_BOT_TOKEN`.)

### Step 7 — Configure Loom and run the worker

Put both tokens in `.env` (or re-run `bash setup.sh`):

```bash
LOOM_SLACK_BOT_TOKEN=xoxb-...      # from step 6
LOOM_SLACK_APP_TOKEN=xapp-...      # from step 2
# Enable the gatekeeper "decide what to save" brain (any one of these):
LOOM_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
```

The Slack worker talks to the API service, so run **both**:

```bash
.venv/bin/loom serve     # API service (one terminal)
.venv/bin/loom slack     # Slack worker (another terminal)

# or, to also answer @mentions and DMs:
.venv/bin/loom slack --interactive
```

### Step 8 — Invite the bot to a channel

In Slack, go to the channel and type:

```
/invite @Loom
```

That's it — the bot is now silently reading that channel.

### Example: what actually happens

Suppose your team chats in `#engineering`:

```
@alice:  we keep getting bitten by blocking DB calls in the API
@bob:    yeah let's make it a rule — always use asyncpg, never psycopg2 in routes
@alice:  agreed, async everywhere for I/O
```

1. The bot reads all three messages silently (posts nothing).
2. ~3 minutes after the thread goes quiet, the gatekeeper LLM (DeepSeek) reads the
   exchange and decides it's a durable convention worth keeping.
3. It saves a summary to your database, e.g.
   *domain: `coding` — "Use async/await for all I/O; use asyncpg, never blocking
   psycopg2 in API routes."*
4. **Later, anyone with the same Loom MCP benefits automatically.** When a
   teammate opens Cursor and starts a task, their agent calls `session_init` and
   receives that rule in its context — without anyone re-explaining it.

You can also pull on demand, right inside Slack:

```
/loom-recall database access
→ (private reply) [HIGH] coding/convention: Use async/await for all I/O ...
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
