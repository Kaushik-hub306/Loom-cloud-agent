# Loom Cloud Agent

Shared memory for AI agents. Cursor does the work. Loom remembers it. Every agent picks up where you left off.

```
Cursor does the work → Loom remembers it → Next session, Cursor already knows
```

## What you need before starting

- A computer (Mac, Windows, or Linux) with Python 3.10+
- A free Supabase account (needs credit card for verification, but $0 — they don't charge)
- A Slack workspace where you can install apps
- 20 minutes

---

## Step-by-step setup (do these in order)

### 1. Create a Supabase database (5 min)

1. Go to [supabase.com](https://supabase.com) → Sign up → **New project**
2. Fill in:
   - **Name:** `loom-memory` (or whatever you want)
   - **Database password:** click "Generate a password" → **save it somewhere** (you'll need it)
   - **Region:** pick the one closest to you
3. Click **Create project** → wait 2 minutes for the database to spin up
4. Once it's ready, go to: **Project Settings → Database** (in the left sidebar)
5. Find the section called **Connection String** → click the **URI** tab
6. Copy the entire string. It looks like:
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.xxxxxx.supabase.co:5432/postgres
   ```
7. Save this string. It's the single most important piece. Everything connects through this.

**What this gives you:** A hosted Postgres database. Free tier includes 500MB — enough for millions of rules and conversation summaries.

---

### 2. Create a Slack bot (5 min)

Skip this section if you only want MCP without Slack. The bot is how Loom reads your conversations with Cursor.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From Scratch**
2. Fill in:
   - **App Name:** `Loom Memory`
   - **Workspace:** pick your Slack workspace
3. Click **Create App**

#### Enable Socket Mode

4. In the left sidebar, click **Socket Mode** → toggle it **ON**
5. A popup appears: "Generate an app-level token"
   - Token name: `socket`
   - Click **Generate**
   - Copy the token that appears (starts with `xapp-`). **Save this. You can only see it once.**
6. Click **Done**

#### Add permissions

7. In the left sidebar, click **OAuth & Permissions**
8. Scroll down to **Scopes → Bot Token Scopes**. Click **Add an OAuth Scope**. Add these four scopes, one at a time:
   - `channels:history` — lets the bot read messages in channels
   - `app_mentions:read` — lets the bot see when someone @mentions it
   - `chat:write` — lets the bot post messages (required for Socket Mode to work, even in silent mode)
   - `im:history` — lets the bot read direct messages
9. Scroll up to **OAuth Tokens for Your Workspace** → click **Install to [Your Workspace]**
10. Review the permissions → click **Allow**
11. Copy the **Bot User OAuth Token** (starts with `xoxb-`). **Save this too.**

#### Invite the bot to channels

12. Open Slack. In each channel where Cursor operates, type:
    ```
    /invite @Loom Memory
    ```
13. The bot joins the channel. It reads everything. It never responds (because of `LOOM_SILENT=true`).

**What this gives you:** A Slack app that can read messages in channels. Two tokens: `xapp-` (app-level, for Socket Mode) and `xoxb-` (bot user, for OAuth).

---

### 3. Clone and run setup (2 min)

```bash
git clone https://github.com/Kaushik-hub306/Loom-cloud-agent.git
cd Loom-cloud-agent
./setup.sh
```

The script will ask you three questions:

**Question 1:** `Paste your Supabase URI:`
→ Paste the connection string from **Step 1, point 6**

**Question 2:** `Set up Slack bot? (y/N):`
→ Type `y` if you did Step 2. Type `n` if you skipped it.

**Question 3 (only if you chose `y`):**
→ `Slack Bot Token (xoxb-...):` — paste from Step 2, point 11
→ `Slack App Token (xapp-...):` — paste from Step 2, point 5

The script then automatically:
- Installs all Python dependencies
- Creates all database tables (`rules`, `conversation_contexts`, `conversation_blobs`)
- Enables the pgvector extension (for semantic search)
- Creates HNSW indexes (for fast vector similarity search)
- Generates an MCP config file at `~/.claude/loom-mcp-config.json`
- Tests the database connection
- Saves all environment variables to a `.env` file

**What this gives you:** A fully provisioned Loom Cloud Agent. Database ready. MCP config ready.

---

### 4. Connect Cursor to Loom (1 min)

Loom is an MCP server. Cursor connects to it via MCP. You need to add Loom to Cursor's configuration.

#### If you use Cursor IDE:

Open or create `.cursor/mcp.json` in your project folder:
```json
{
  "mcpServers": {
    "loom": {
      "command": "python3",
      "args": ["/full/path/to/Loom-cloud-agent/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://postgres:...@db....supabase.co:5432/postgres"
      }
    }
  }
}
```

Replace:
- `/full/path/to/Loom-cloud-agent` with the actual path where you cloned the repo
- The `LOOM_DATABASE_URL` with your Supabase URI from Step 1

You can find the full path by running `pwd` inside the cloned folder.

#### If you use Claude Code:

Add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "loom": {
      "command": "python3",
      "args": ["/full/path/to/Loom-cloud-agent/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://postgres:...@db....supabase.co:5432/postgres"
      }
    }
  }
}
```

#### If you use Claude Desktop:

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):
```json
{
  "mcpServers": {
    "loom": {
      "command": "python3",
      "args": ["/full/path/to/Loom-cloud-agent/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://postgres:...@db....supabase.co:5432/postgres"
      }
    }
  }
}
```

Windows: `%APPDATA%\Claude\claude_desktop_config.json`
Linux: `~/.config/Claude/claude_desktop_config.json`

#### For any other MCP-compatible tool:

Same pattern — add `loom` to the `mcpServers` block with the command, args, and env shown above.

**What this gives you:** Cursor (or Claude Code, or any MCP agent) now auto-calls Loom's `session_init` on every conversation start. It gets conventions + past conversation context injected into its system prompt.

---

### 5. Restart Cursor

Close and reopen Cursor (or Claude Code, or Claude Desktop). The MCP connection initializes on startup.

---

### 6. Deploy the Slack listener (optional, 5 min)

The MCP server works locally — Cursor connects to it on your machine. But the Slack bot (the silent listener that captures conversations) only runs when your computer is on.

To keep it running 24/7, deploy to Railway (free tier):

1. Push the cloned repo to your own GitHub account:
   ```bash
   git remote add myrepo https://github.com/YOUR_USERNAME/Loom-cloud-agent.git
   git push myrepo main
   ```

2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub** → select your repo

3. Add environment variables in Railway (**Environment → Variables**):
   ```
   DATABASE_URL    = postgresql://postgres:...@db....supabase.co:5432/postgres
   SLACK_BOT_TOKEN = xoxb-...
   SLACK_APP_TOKEN = xapp-...
   LOOM_SILENT     = true
   LOOM_LLM_MODEL  = gemini
   ```

4. Click **Deploy**. Railway gives you a URL. The bot automatically connects to Slack and starts listening.

**Alternative (run locally):**
```bash
source .env
python3 run.py
```
Keep the terminal open. The bot runs as long as your computer is on.

**What this gives you:** Always-on conversation capture. Loom reads every message with Cursor, extracts what's important, and saves it. Doesn't matter if your computer is asleep.

---

### 7. Verify everything works (2 min)

#### Test MCP (Cursor / Claude Code):

1. Open Cursor → start a new conversation
2. Type: "What conventions does my team have?"
3. If the database is empty (first time), Loom responds: "No conventions found. Teach some!"
4. That response proves the connection works — Cursor called `session_init`, Loom responded

#### Teach a rule:

1. Type in Cursor: "teach coding:convention Use type hints on all public functions"
2. Cursor calls Loom's `teach` tool → stores the rule in Supabase with an embedding
3. Next conversation → Cursor gets this rule in its context

#### Test Slack capture (if you set up Slack):

1. In a channel where both `@Cursor` and `@Loom Memory` are present, have a meaningful conversation with Cursor
2. Wait 3 minutes (the gatekeeper debounce)
3. The LLM evaluates whether the conversation is worth remembering
4. Next time you start a Cursor conversation, you'll see a "Recent Context" section in the system prompt with the summary

---

## How it works (the short version)

```
You talk to Cursor in Slack (this doesn't change — Cursor is still your bot)

Behind the scenes, automatically:
  1. Cursor calls Loom session_init → gets your team's conventions + past conversations
  2. Loom reads every message in the channel (silently, never responds)
  3. After 3 minutes of quiet, the LLM decides: "remember this" or "nothing important"
  4. Worthwhile conversations are summarized and saved to Supabase
  5. Next session: Cursor gets everything — rules, past context, decisions

You never do anything extra. No /save commands. No separate bot to talk to.
Cursor is the bot. Loom is the memory. That's it.
```

Both bots in the same channel:

```
#general
  ├── @Cursor        ← talks to you, does work (the bot you interact with)
  └── @Loom Memory   ← reads everything, never speaks (silent memory)
```

## The database

Two tables store everything:

| Table | What it stores | Expires after |
|-------|---------------|---------------|
| `rules` | Team conventions, patterns, best practices | Never (auto-decays if not reinforced) |
| `conversation_contexts` | LLM-generated summaries of important conversations | 30 days |
| `conversation_blobs` | Raw message backup (only used if LLM fails) | 14 days |

All tables have pgvector embeddings + HNSW indexes for fast semantic search.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LOOM_DATABASE_URL` | Yes | Your Supabase Postgres URI |
| `SLACK_BOT_TOKEN` | For Slack | Bot User OAuth Token (starts with `xoxb-`) |
| `SLACK_APP_TOKEN` | For Slack | App-Level Token for Socket Mode (starts with `xapp-`) |
| `LOOM_SILENT` | No | Set to `true` for silent mode. The bot reads everything, never responds |
| `LOOM_LLM_MODEL` | No | LLM for the gatekeeper. Options: `gemini`, `claude`, `deepseek`, `chatgpt`. Default: `deepseek` |

## Tech

Supabase + pgvector | Python | FastAPI | Slack Bolt | MCP | LiteLLM
