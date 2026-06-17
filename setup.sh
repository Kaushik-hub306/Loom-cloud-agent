#!/usr/bin/env bash
set -e

# ── Loom Cloud Agent — One-Command Setup ─────────────────────────────
# Your friend clones the repo. Runs ./setup.sh. Done.
#
# What it does:
#   1. Installs Python dependencies
#   2. Asks for Supabase URL (or they paste it)
#   3. Provisions the database (pgvector + tables + indexes)
#   4. Asks for Slack tokens (or skips if they don't want Slack)
#   5. Generates MCP config for Cursor / Claude Code / Codex
#   6. Tests the connection
#   7. Prints next steps

# Resolve the script's directory so paths work regardless of where it's called from
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
BLUE="\033[34m"
RED="\033[31m"
RESET="\033[0m"

echo ""
echo -e "${BOLD}============================================${RESET}"
echo -e "${BOLD}   Loom Cloud Agent — Setup${RESET}"
echo -e "${BOLD}   Shared memory for AI agents${RESET}"
echo -e "${BOLD}============================================${RESET}"
echo ""

# ── Step 1: Install dependencies ─────────────────────────────────────

echo -e "${BOLD}Step 1/5: Installing dependencies...${RESET}"
pip install -r requirements.txt --quiet 2>/dev/null || pip3 install -r requirements.txt --quiet
echo -e "${GREEN}✓ Dependencies installed${RESET}"
echo ""

# ── Step 2: Supabase URL ─────────────────────────────────────────────

echo -e "${BOLD}Step 2/5: Supabase database${RESET}"
echo ""
echo "Loom needs a Postgres database. The easiest way:"
echo "  1. Go to supabase.com → Create a free project"
echo "  2. Project Settings → Database → Connection String → URI tab"
echo "  3. Copy the URI (starts with postgresql://)"
echo ""

if [ -n "$LOOM_DATABASE_URL" ]; then
    DB_URL="$LOOM_DATABASE_URL"
    echo -e "${GREEN}Using LOOM_DATABASE_URL from environment${RESET}"
else
    read -p "Paste your Supabase URI: " DB_URL
    if [ -z "$DB_URL" ]; then
        echo -e "${RED}Database URL is required. Exiting.${RESET}"
        exit 1
    fi
fi

# Export for the session
export LOOM_DATABASE_URL="$DB_URL"
export DATABASE_URL="$DB_URL"
echo ""

# ── Step 3: Provision database ────────────────────────────────────────

echo -e "${BOLD}Step 3/5: Provisioning database...${RESET}"

# Create pgvector extension and run schema
export SCHEMA_FILE="$SCRIPT_DIR/supabase/schema.sql"
python3 -c "
import psycopg2, os, sys
url = os.environ['LOOM_DATABASE_URL']
schema_file = os.environ['SCHEMA_FILE']
try:
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()

    # Enable pgvector
    cur.execute('CREATE EXTENSION IF NOT EXISTS vector;')
    print('  ✓ pgvector extension enabled')

    # Run the schema
    if os.path.exists(schema_file):
        cur.execute(open(schema_file).read())
        print(f'  ✓ Schema provisioned')
    else:
        print(f'  ✗ Schema file not found: {schema_file}')
        sys.exit(1)
    conn.commit()
    cur.close()
    conn.close()
    print('  ✓ Database ready')
except Exception as e:
    print(f'  ✗ Database connection failed: {e}')
    print('  Check your Supabase URI. Make sure it starts with postgresql://')
    import traceback; traceback.print_exc()
    sys.exit(1)
" 2>&1

echo -e "${GREEN}✓ Database provisioned${RESET}"
echo ""

# ── Step 4: Slack tokens (optional) ──────────────────────────────────

echo -e "${BOLD}Step 4/5: Slack setup (optional — skip if you only want MCP)${RESET}"
echo ""
echo "To capture conversations from Slack, Loom needs a Slack bot."
echo "If you skip this, Loom still works as an MCP server for Cursor/Claude Code."
echo ""

read -p "Set up Slack bot? (y/N): " SETUP_SLACK
if [ "$SETUP_SLACK" = "y" ] || [ "$SETUP_SLACK" = "Y" ]; then
    echo ""
    echo "You'll need two tokens from api.slack.com/apps:"
    echo "  1. Create a new app → Socket Mode (ON)"
    echo "  2. OAuth & Permissions → add scopes:"
    echo "     channels:history, app_mentions:read, chat:write, im:history"
    echo "  3. Install to workspace → copy Bot User OAuth Token (xoxb-...)"
    echo "  4. Basic Information → App-Level Tokens → connections:write → copy token (xapp-...)"
    echo ""

    read -p "Slack Bot Token (xoxb-...): " SLACK_BOT_TOKEN
    read -p "Slack App Token (xapp-...): " SLACK_APP_TOKEN

    if [ -n "$SLACK_BOT_TOKEN" ] && [ -n "$SLACK_APP_TOKEN" ]; then
        export SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN"
        export SLACK_APP_TOKEN="$SLACK_APP_TOKEN"
        echo -e "${GREEN}✓ Slack tokens saved${RESET}"
    else
        echo -e "${YELLOW}⚠ Tokens empty — Slack bot won't start. You can add them later.${RESET}"
    fi
else
    echo -e "${YELLOW}Skipping Slack setup. Conversation capture won't work, but MCP will.${RESET}"
fi
echo ""

# ── Step 5: Generate MCP config ───────────────────────────────────────

echo -e "${BOLD}Step 5/5: MCP config for Cursor / Claude Code${RESET}"
echo ""

PYTHON_PATH=$(which python3 || which python)
MCP_DIR="$SCRIPT_DIR"
CONFIG_FILE="$HOME/.claude/loom-mcp-config.json"

cat > "$CONFIG_FILE" << EOF
{
  "mcpServers": {
    "loom": {
      "command": "$PYTHON_PATH",
      "args": ["$MCP_DIR/memory_agent/mcp_server.py"],
      "env": {
        "LOOM_DATABASE_URL": "$DB_URL"
      }
    }
  }
}
EOF

echo -e "${GREEN}✓ MCP config generated at: $CONFIG_FILE${RESET}"
echo ""

# ── Test connection ──────────────────────────────────────────────────

echo -e "${BOLD}Testing connection...${RESET}"
python3 -c "
import psycopg2, os
try:
    conn = psycopg2.connect(os.environ['LOOM_DATABASE_URL'])
    cur = conn.cursor()
    cur.execute(\"SELECT COUNT(*) FROM rules;\")
    rules = cur.fetchone()[0]
    cur.execute(\"SELECT COUNT(*) FROM conversation_contexts;\")
    ctx_count = cur.fetchone()[0]
    print(f'  ✓ Connected — {rules} rules, {ctx_count} context summaries')
    cur.close()
    conn.close()
except Exception as e:
    print(f'  ✗ Connection test failed: {e}')
" 2>&1
echo ""

# ── Done ──────────────────────────────────────────────────────────────

echo -e "${BOLD}============================================${RESET}"
echo -e "${GREEN}${BOLD}   Setup complete!${RESET}"
echo -e "${BOLD}============================================${RESET}"
echo ""

# Detect which tools the user has installed
HAS_CURSOR=false
HAS_CLAUDE_CODE=false
HAS_CLAUDE_DESKTOP=false
[ -d "/Applications/Cursor.app" ] && HAS_CURSOR=true
[ -f "$HOME/.claude/settings.json" ] && HAS_CLAUDE_CODE=true
[ -f "$HOME/Library/Application Support/Claude/claude_desktop_config.json" ] && HAS_CLAUDE_DESKTOP=true

# Show the actual MCP config content
echo -e "${BOLD}Your MCP config (save this):${RESET}"
echo ""
echo -e "${BLUE}────────────────────────────────────────────${RESET}"
cat "$CONFIG_FILE"
echo -e "${BLUE}────────────────────────────────────────────${RESET}"
echo ""

echo -e "${BOLD}Now add it to your AI tool:${RESET}"
echo ""

if $HAS_CURSOR; then
    echo -e "  ${BOLD}Cursor IDE (detected):${RESET}"
    echo "    1. Open Cursor"
    echo "    2. In your project, create or open .cursor/mcp.json"
    echo "    3. Paste the JSON above into that file"
    echo "    4. Restart Cursor"
    echo ""
fi

if $HAS_CLAUDE_CODE; then
    echo -e "  ${BOLD}Claude Code (detected):${RESET}"
    echo "    Run this command:"
    echo ""
    echo -e "    ${GREEN}cat $CONFIG_FILE >> ~/.claude/settings.json${RESET}"
    echo "    Then restart Claude Code."
    echo ""
fi

if $HAS_CLAUDE_DESKTOP; then
    echo -e "  ${BOLD}Claude Desktop (detected):${RESET}"
    echo "    1. Open: ~/Library/Application Support/Claude/claude_desktop_config.json"
    echo "    2. Add the 'loom' block from the JSON above into the 'mcpServers' section"
    echo "    3. Restart Claude Desktop"
    echo ""
fi

if ! $HAS_CURSOR && ! $HAS_CLAUDE_CODE && ! $HAS_CLAUDE_DESKTOP; then
    echo -e "  ${YELLOW}No AI tools detected on this machine.${RESET}"
    echo "  Copy the JSON above into your tool's MCP config:"
    echo ""
    echo "  Cursor IDE:       .cursor/mcp.json in your project"
    echo "  Claude Code:      ~/.claude/settings.json"
    echo "  Claude Desktop:   ~/Library/Application Support/Claude/claude_desktop_config.json"
    echo "  Codex CLI:        ~/.codex/config.toml or ~/.codex/config.json"
    echo ""
fi

echo -e "  ${BOLD}2. Start the Slack listener:${RESET}"
echo ""

if [ "$SETUP_SLACK" = "y" ] || [ "$SETUP_SLACK" = "Y" ]; then
    echo -e "     ${YELLOW}Locally (your Mac, right now):${RESET}"
    echo "       cd $SCRIPT_DIR"
    echo "       source .env"
    echo "       python3 run.py"
    echo ""
    echo "     This starts the Slack bot. It connects to your workspace"
    echo "     and reads messages in channels where you /invite'd it."
    echo "     It runs as long as this terminal stays open."
    echo ""
    echo -e "     ${YELLOW}24/7 on Railway (free):${RESET}"
    echo "       1. Push this repo to your GitHub"
    echo "       2. railway.app → New Project → Deploy from GitHub"
    echo "       3. Add env vars: DATABASE_URL, SLACK_BOT_TOKEN, SLACK_APP_TOKEN, LOOM_SILENT=true"
    echo "       4. Deploy. Always on, even when your Mac is asleep."
    echo ""
    echo -e "     ${YELLOW}Invite the bot to channels:${RESET}"
    echo "       In Slack: /invite @Loom Memory"
    echo "       Do this in every channel where you talk to Cursor."
else
    echo "     You skipped Slack setup. The MCP server still works."
    echo "     Re-run setup.sh and choose 'y' if you want Slack later."
fi

echo ""
echo -e "  ${BOLD}3. Verify it works:${RESET}"
echo ""
echo "     Open Cursor / Claude Code. Start a new conversation."
echo "     First tool call auto-loads Loom context."
echo "     You'll see this in the system prompt:"
echo ""
echo -e "     ${BLUE}<!-- LOOM:AUTO_CONTEXT -->${RESET}"
echo "     ## Relevant Conventions (from Loom)"
echo ""
echo -e "     ${BOLD}To teach a convention:${RESET}"
echo '       Type: "teach coding:convention Use type hints on all public functions"'
echo ""

# ── Save .env ───────────────────────────────────────────────────────

ENV_FILE="$SCRIPT_DIR/.env"
cat > "$ENV_FILE" << EOF
export LOOM_DATABASE_URL=$DB_URL
export DATABASE_URL=$DB_URL
EOF
if [ -n "$SLACK_BOT_TOKEN" ]; then
    echo "export SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN" >> "$ENV_FILE"
    echo "export SLACK_APP_TOKEN=$SLACK_APP_TOKEN" >> "$ENV_FILE"
fi
echo "export LOOM_SILENT=true" >> "$ENV_FILE"
echo -e "${GREEN}✓ Environment saved to $ENV_FILE${RESET}"
