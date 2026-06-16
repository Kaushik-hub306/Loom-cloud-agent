"""
MCP Server — connects Claude Code / Codex / Cursor to shared team memory.
Add to Claude Code: claude mcp add loom-memory -- python3 path/to/mcp_server.py
"""
import os
import sys
import json
import psycopg2
from pathlib import Path

# ── Shared memory loader ───────────────────────────────────

def get_rules(task: str = "", domain: str = "", min_confidence: int = 5, limit: int = 15):
    """Load team conventions from Supabase. Semantic if embedding available, text fallback."""
    db_url = os.environ.get("LOOM_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        return []

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    if task:
        rows = []
        # Try semantic search first
        try:
            from litellm import embedding
            result = embedding(model="gemini/text-embedding-004", input=[task[:3000]])
            vec = json.dumps(result.data[0]["embedding"])
            cur.execute("""
                SELECT domain, rule_type, rule, example, confidence,
                       1 - (embedding <=> %s::vector) AS score
                FROM rules
                WHERE embedding IS NOT NULL AND confidence >= %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
            """, (vec, min_confidence, vec, limit))
            rows = cur.fetchall()
        except Exception:
            pass
        # Text fallback: search across rule text, domain, and rule_type
        if not rows:
            cur.execute("""
                SELECT domain, rule_type, rule, example, confidence, 0.0
                FROM rules
                WHERE confidence >= %s
                  AND (LOWER(rule) LIKE %s OR LOWER(domain) LIKE %s OR LOWER(rule_type) LIKE %s)
                ORDER BY confidence DESC LIMIT %s;
            """, (min_confidence, f'%{task.lower()}%', f'%{task.lower()}%', f'%{task.lower()}%', limit))
            rows = cur.fetchall()
        # Domain fallback: still nothing → return top rules from common domains
        if not rows:
            cur.execute("""
                SELECT domain, rule_type, rule, example, confidence, 0.0
                FROM rules WHERE confidence >= %s
                  AND domain IN ('coding','architecture','security','testing','process')
                ORDER BY confidence DESC LIMIT %s;
            """, (min_confidence, limit))
            rows = cur.fetchall()
    elif domain:
        cur.execute("""
            SELECT domain, rule_type, rule, example, confidence, 0.0
            FROM rules WHERE domain = %s AND confidence >= %s
            ORDER BY confidence DESC LIMIT %s;
        """, (domain, min_confidence, limit))
        rows = cur.fetchall()
    else:
        cur.execute("""
            SELECT domain, rule_type, rule, example, confidence, 0.0
            FROM rules WHERE confidence >= %s
            ORDER BY confidence DESC LIMIT %s;
        """, (min_confidence, limit))
        rows = cur.fetchall()

    rules = []
    for row in rows:
        rules.append({
            "domain": row[0],
            "rule_type": row[1],
            "rule": row[2],
            "example": row[3] or "",
            "confidence": row[4],
        })

    cur.close()
    conn.close()
    return rules


def format_rules_for_prompt(rules: list, task: str = "") -> str:
    """Format rules into a system prompt block Claude Code reads."""
    if not rules:
        return ""

    by_domain = {}
    for r in rules:
        by_domain.setdefault(r["domain"], []).append(r)

    lines = ["## Team Conventions (shared memory)", ""]
    lines.append(f"Loaded {len(rules)} relevant conventions. Follow these rules.")
    lines.append("")

    for domain, items in sorted(by_domain.items()):
        lines.append(f"### {domain.replace('_', ' ').title()}")
        for r in items:
            conf = "HIGH" if r["confidence"] >= 7 else "MED" if r["confidence"] >= 4 else "LOW"
            lines.append(f"- [{conf}] [{r['rule_type']}] {r['rule']}")
            if r["example"]:
                lines.append(f"  Example: {r['example']}")
        lines.append("")

    lines.append("You must follow these conventions unless told otherwise.")
    return "\n".join(lines)


# ── MCP Server (stdio transport) ──────────────────────────

import sys
import json as _json


def handle_request(request: dict) -> dict | None:
    """Handle a single JSON-RPC MCP request."""
    method = request.get("method", "")
    req_id = request.get("id")

    # ── initialize ──
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "loom-memory",
                    "version": "0.1.0",
                }
            }
        }

    # ── notifications (no response) ──
    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        return None

    # ── tools/list ──
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "session_init",
                        "description": "Load team conventions and rules before starting a coding session. Call this FIRST. Returns formatted context to include in your system prompt.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "task": {
                                    "type": "string",
                                    "description": "What you're about to work on. Used to find relevant conventions."
                                },
                                "domain": {
                                    "type": "string",
                                    "description": "Optional: filter by domain (coding, architecture, security, testing, etc.)"
                                }
                            },
                            "required": ["task"]
                        }
                    },
                    {
                        "name": "recall_relevant",
                        "description": "Search team memory for specific conventions, patterns, or decisions.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "What to search for. Returns matching rules sorted by relevance."
                                },
                                "domain": {
                                    "type": "string",
                                    "description": "Optional: filter by domain"
                                }
                            },
                            "required": ["query"]
                        }
                    },
                    {
                        "name": "get_domain_rules",
                        "description": "Get all conventions in a specific domain (coding, architecture, testing, security, etc.)",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "domain": {
                                    "type": "string",
                                    "description": "Domain to fetch rules from"
                                }
                            },
                            "required": ["domain"]
                        }
                    }
                ]
            }
        }

    # ── tools/call ──
    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name == "session_init":
            task = arguments.get("task", "")
            domain = arguments.get("domain", "")
            rules = get_rules(task=task, domain=domain)
            text = format_rules_for_prompt(rules, task)
            if not text:
                text = f"No team conventions found for '{task}'. Teach some in Slack with /teach!"

        elif tool_name == "recall_relevant":
            query = arguments.get("query", "")
            domain = arguments.get("domain", "")
            rules = get_rules(task=query, domain=domain)
            if not rules:
                text = f"No results for '{query}'. Try /teach in Slack to add conventions."
            else:
                lines = [f"## Found {len(rules)} conventions", ""]
                for r in rules:
                    lines.append(f"- [{r['confidence']}/10] [{r['domain']}] {r['rule']}")
                text = "\n".join(lines)

        elif tool_name == "get_domain_rules":
            domain = arguments.get("domain", "coding")
            rules = get_rules(domain=domain)
            if not rules:
                text = f"No rules in domain '{domain}'."
            else:
                lines = [f"## {len(rules)} conventions in {domain}", ""]
                for r in rules:
                    lines.append(f"- [{r['confidence']}/10] [{r['rule_type']}] {r['rule']}")
                    if r["example"]:
                        lines.append(f"  Example: {r['example']}")
                text = "\n".join(lines)

        else:
            text = f"Unknown tool: {tool_name}"

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}]
            }
        }

    return None


def main():
    """MCP stdio loop."""
    print("[loom-mcp] Starting MCP server...", file=sys.stderr)
    print(f"[loom-mcp] DB: {'connected' if os.environ.get('LOOM_DATABASE_URL') or os.environ.get('DATABASE_URL') else 'not set'}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = _json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(_json.dumps(response) + "\n")
                sys.stdout.flush()
        except Exception as e:
            print(f"[loom-mcp] error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
