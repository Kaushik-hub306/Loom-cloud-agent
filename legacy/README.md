# Loom Cloud Agent

Shared memory for AI agents. Cursor does the work. Loom remembers it. Every agent picks up where you left off.

## What you need before starting

- A computer (Mac, Windows, or Linux) with **Python 3.10 or newer**
- A free **Supabase** account (needs credit card for verification — $0, they don't charge)
- A free **Slack** workspace where you can install apps
- An **LLM API key** — pick one:
  - **DeepSeek** (cheapest, easiest): get a key at [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) — starts with `sk-`
  - **Gemini** (free tier available): get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  - **Claude/Anthropic**: get a key at [console.anthropic.com](https://console.anthropic.com)
- **20 minutes**

---

## Step 1: Get your LLM API key

Loom uses an LLM to read your Slack conversations and decide what's worth remembering. Without an API key, the memory feature silently does nothing.

Pick one provider. DeepSeek is the cheapest and easiest to start with.

### Option A: DeepSeek (recommended — $0.14 per million tokens)

1. Go to [platform.deepseek.com](https://platform.deepseek.com) → Sign up
2. Click **API Keys** in the left sidebar
3. Click **Create new API key** → name it `loom` → copy the key
4. It starts with `sk-`. Save it. You can only see it once.

### Option B: Gemini (free tier available)

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Click **Create API Key** → copy the key
3. Gemini's free tier gives you 1,500 requests per day — enough for personal use

### Option C: Claude / Anthropic

1. Go to [console.anthropic.com](https://console.anthropic.com) → Sign up
2. Click **API Keys** → **Create Key** → copy it
3. Starts with `sk-ant-`

**Save your key.** You'll paste it into the `.env` file later.

---

## Step 2: Create a Supabase database

1. Go to [supabase.com](https://supabase.com) → Sign up → **New project**
2. Fill in:
   - **Name:** `loom-memory` (or whatever you want)
   - **Database password:** click **Generate a password** → **write it down somewhere** — you'll need it later
   - **Region:** pick the one closest to you
3. Click **Create project** → wait 2 minutes for it to spin up
4. Once ready, go to: **Project Settings → Database** (left sidebar)
5. Find **Connection String** → click the **URI** tab
6. Copy the entire string. It looks like:
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.xxxxxx.supabase.co:5432/postgres
   ```
7. If the password shows as `[YOUR-PASSWORD]`, replace it with the actual password you wrote down in step 2
8. Save this string. Everything connects through it.

---

## Step 3: Create a Slack bot

Loom reads messages in your Slack channels through a bot. It can run silently (reads everything, never responds — you talk to Cursor, Loom just watches) or interactively (responds to @mentions).

### 3a. Create the app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From Scratch**
2. Fill in:
   - **App Name:** `Loom agent` (or whatever you want)
   - **Workspace:** pick your Slack workspace
3. Click **Create App**

### 3b. Enable Socket Mode

4. In the left sidebar, click **Socket Mode** → toggle it **ON**
5. A popup asks "Generate an app-level token":
   - Token name: `socket`
   - Click **Generate**
   - **Copy the token** that appears. It starts with `xapp-`. Save it. You can only see it once.
6. Click **Done**

### 3c. Add OAuth permissions (scopes)

7. In the left sidebar, click **OAuth & Permissions**
8. Scroll down to **Scopes → Bot Token Scopes**. Click **Add an OAuth Scope**. Add all five, one at a time:
   - `channels:history` — lets the bot read messages in channels
   - `channels:read` — lets the bot see channel info
   - `app_mentions:read` — lets the bot see when someone @mentions it
   - `chat:write` — lets the bot post messages (required even in silent mode)
   - `im:history` — lets the bot read direct messages

### 3d. Subscribe to events

9. In the left sidebar, click **Event Subscriptions**
10. Under **Subscribe to bot events**, click **Add Bot User Event**
11. Type `message.channels` → select it from the dropdown
12. Click **Save Changes** (green button at the bottom)

### 3e. Install to workspace

13. In the left sidebar, click **OAuth & Permissions**
14. Scroll up to **OAuth Tokens for Your Workspace** → click **Install to [Your Workspace]**
15. Review the permissions → click **Allow**
16. **Copy the Bot User OAuth Token**. It starts with `xoxb-`. Save it. You can only see it once.

### 3f. Generate an app-level token (if not done in 3b)

Verify: in **Basic Information** → **App-Level Tokens**, you should see one token with scope `connections:write`. If not, click **Generate Token and Scopes**, give it `connections:write`, and copy the `xapp-` token.

### Summary of what you should have saved:

| Token | Starts with | Where to find it |
|-------|-------------|------------------|
| Bot User OAuth Token | `xoxb-` | OAuth & Permissions → after install |
| App-Level Token | `xapp-` | Basic Information → App-Level Tokens |

---

## Step 4: Clone and run setup

Open your terminal. Run:

```bash
git clone https://github.com/Kaushik-hub306/Loom-cloud-agent.git
cd Loom-cloud-agent
./setup.sh
```

The script asks you questions. Answer them:

**Question 1:** `Paste your Supabase URI:`
→ Paste the `postgresql://...` string from Step 2

**Question 2:** `Set up Slack bot? (y/N):`
→ Type `y`

**Question 3:** `Slack Bot Token (xoxb-...):`
→ Paste the `xoxb-` token from Step 3e

**Question 4:** `Slack App Token (xapp-...):`
→ Paste the `xapp-` token from Step 3b

The script then:
- Installs all Python packages
- Creates the database tables (`rules`, `conversation_contexts`, `conversation_blobs`)
- Enables pgvector (for semantic search)
- Creates HNSW indexes (for fast vector similarity)
- Generates an MCP config file at `~/.claude/loom-mcp-config.json`
- Tests the connection
- Creates a `.env` file

---

## Step 5: Add your LLM API key

Open the `.env` file that was just created:

```bash
nano .env
```

Add your LLM key. Pick one:

```bash
# For DeepSeek:
export DEEPSEEK_API_KEY=sk-your-key-here
export LOOM_LLM_MODEL=deepseek

# For Gemini:
export GEMINI_API_KEY=your-key-here
export LOOM_LLM_MODEL=gemini

# For Claude:
export ANTHROPIC_API_KEY=sk-ant-your-key-here
export LOOM_LLM_MODEL=claude
```

Save the file. The full `.env` should now look something like:

```bash
export LOOM_DATABASE_URL=postgresql://postgres:password@db.xxxxxx.supabase.co:5432/postgres
export DATABASE_URL=postgresql://postgres:password@db.xxxxxx.supabase.co:5432/postgres
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
export LOOM_SILENT=true
export DEEPSEEK_API_KEY=sk-...
export LOOM_LLM_MODEL=deepseek
```

---

## Step 6: Start Loom

```bash
source .env
export SSL_CERT_FILE=$(python3 -c "import certifi; print(certifi.where())")
nohup python3 run.py > /tmp/loom-run.log 2>&1 &
sleep 3
curl -s http://localhost:8000/health
```

You should see `{"status":"ok","backend":"postgres"}`.

To check the full log at any time: `tail -20 /tmp/loom-run.log`

To stop Loom: `lsof -ti:8000 | xargs kill -9`

---

## Step 7: Invite the bot to Slack channels

For every channel where Cursor operates, type in Slack:

```
/invite @Loom agent
```

Both `@Cursor` and `@Loom agent` must be in the same channel. Cursor talks to you. Loom watches silently.

---

## Step 8: Add Loom to Cursor / Claude Code

Loom is an MCP server. Your AI tool connects to it and auto-loads memory on every session.

### If you use Cursor IDE:

Create or edit `.cursor/mcp.json` in your project folder:

```json
{
  "mcpServers": {
    "loom": {
      "command": "python3",
      "args": ["/full/path/to/Loom-cloud-agent/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://postgres:password@db.xxxxxx.supabase.co:5432/postgres"
      }
    }
  }
}
```

Replace `/full/path/to/Loom-cloud-agent` with the actual path (run `pwd` inside the cloned folder to see it).
Replace the `LOOM_DATABASE_URL` with your Supabase URI.

### If you use Claude Code:

Add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "loom": {
      "command": "python3",
      "args": ["/full/path/to/Loom-cloud-agent/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://..."
      }
    }
  }
}
```

### If you use Claude Desktop (Mac):

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "loom": {
      "command": "python3",
      "args": ["/full/path/to/Loom-cloud-agent/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "postgresql://..."
      }
    }
  }
}
```

Windows: `%APPDATA%\Claude\claude_desktop_config.json`
Linux: `~/.config/Claude/claude_desktop_config.json`

The setup script already saved this config to `~/.claude/loom-mcp-config.json` — you can copy it from there.

Restart Cursor / Claude Code after adding the config.

---

## Step 9: Verify it works

### Test the API directly:

```bash
# Check health
curl -s http://localhost:8000/health

# See stored rules
curl -s http://localhost:8000/stats | python3 -m json.tool

# Test session_init
curl -s -X POST http://localhost:8000/session_init \
  -H "Content-Type: application/json" \
  -d '{"task": "build a REST API"}' | python3 -m json.tool

# Teach a rule
curl -s -X POST http://localhost:8000/teach \
  -H "Content-Type: application/json" \
  -d '{"domain": "coding", "rule_type": "convention", "rule": "Use async/await for all I/O operations", "confidence": 7}'
```

### Test Slack capture:

1. In Slack, type a meaningful message to Cursor in a channel where `@Loom agent` is present
2. Wait 3 minutes (the bot debounces — needs a lull before evaluating)
3. Check if context was saved:
```bash
curl -s http://localhost:8000/stats | python3 -m json.tool | grep conversation
```

---

## Step 10: Deploy 24/7 (optional)

When your computer is off, Loom stops. For always-on memory, deploy to Railway (free tier):

1. Push the repo to your own GitHub account
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Add these environment variables in Railway:
   ```
   DATABASE_URL          = postgresql://...
   SLACK_BOT_TOKEN       = xoxb-...
   SLACK_APP_TOKEN       = xapp-...
   LOOM_SILENT           = true
   LOOM_LLM_MODEL        = deepseek
   DEEPSEEK_API_KEY      = sk-...
   ```
4. Click **Deploy**. Always on, even when your Mac is asleep.

---

## How it works

```
You talk to Cursor in Slack (normal — nothing changes for you)

Behind the scenes:
  Cursor calls Loom session_init → gets your team's conventions + past conversations
  Loom silently reads every message in the channel (never responds)
  After 3 minutes of quiet, the LLM evaluates: "remember this" or "nothing important"
  Worthwhile conversations are summarized + embedded + saved to Supabase
  Next session: any agent gets the full context

You never click a button. You never run a command.
Cursor is the bot. Loom is the memory.
```

Both bots in the same channel:

```
#general
  ├── @Cursor       ← talks to you, does work
  └── @Loom agent   ← 👁️ reads everything, never speaks
```

---

## Environment variables (complete reference)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LOOM_DATABASE_URL` | **Yes** | — | Your Supabase Postgres URI |
| `DATABASE_URL` | **Yes** | — | Same as above (aliased for compatibility) |
| `SLACK_BOT_TOKEN` | For Slack | — | Bot User OAuth Token (starts with `xoxb-`) |
| `SLACK_APP_TOKEN` | For Slack | — | App-Level Token for Socket Mode (starts with `xapp-`) |
| `LOOM_SILENT` | No | `false` | `true` = reads everything, never responds. `false` = responds to @mentions |
| `LOOM_LLM_MODEL` | No | `deepseek` | Which LLM to use: `deepseek`, `gemini`, `claude`, or `chatgpt` |
| `DEEPSEEK_API_KEY` | For DeepSeek | — | Your DeepSeek API key (starts with `sk-`) |
| `GEMINI_API_KEY` | For Gemini | — | Your Gemini API key |
| `ANTHROPIC_API_KEY` | For Claude | — | Your Anthropic API key (starts with `sk-ant-`) |
| `OPENAI_API_KEY` | For ChatGPT | — | Your OpenAI API key |
| `PORT` | No | `8000` | HTTP port for the API |
| `MEMORY_AGENT_URL` | No | `http://localhost:8000` | URL for the Slack bot to reach the API |

---

## The database

| Table | What it stores | Expires after |
|-------|---------------|---------------|
| `rules` | Team conventions, patterns, best practices | Never (auto-decays if not reinforced) |
| `conversation_contexts` | LLM-generated summaries of important conversations | 30 days |
| `conversation_blobs` | Raw message backup (only used if LLM fails) | 14 days |

All tables use pgvector embeddings + HNSW indexes for fast semantic search.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `LOOM_DATABASE_URL is required` | Your `.env` file is missing or not sourced. Run `source .env` before starting |
| `password authentication failed` | The password in your Supabase URI is wrong. Go to Supabase → Project Settings → Database → Reset password |
| Bot doesn't receive messages | Run through Step 3 completely — event subscription `message.channels` is the most commonly missed step |
| Bot receives messages but nothing is saved | Make sure `LOOM_LLM_MODEL` and the corresponding API key are set in `.env` |
| `address already in use` | Another instance is running. Run `lsof -ti:8000 \| xargs kill -9` to stop it |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Run `export SSL_CERT_FILE=$(python3 -c "import certifi; print(certifi.where())")` before starting |
| `Bolt app is running!` but no messages | Reinstall the Slack app (Step 3e) and re-invite the bot to channels |
| `pip install` fails | Try `pip3 install -r requirements.txt` instead |

---

## Tech

Supabase + pgvector | Python | FastAPI | Slack Bolt (Socket Mode) | MCP | LiteLLM
