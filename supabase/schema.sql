-- Loom Memory Agent schema for Supabase/Postgres
-- HNSW index from day one for fast semantic recall

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Memories table: one row per taught rule
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    domain      TEXT NOT NULL DEFAULT 'general',
    rule_type   TEXT NOT NULL DEFAULT 'convention',
    rule        TEXT NOT NULL,
    example     TEXT DEFAULT '',
    confidence  INTEGER NOT NULL DEFAULT 5 CHECK (confidence >= 1 AND confidence <= 10),
    sources     JSONB DEFAULT '[]',
    source_type TEXT DEFAULT 'user_teach',
    embedding   VECTOR(768),  -- Gemini text-embedding-004 dimension
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for fast vector similarity search
-- Cosine distance is standard for text embeddings
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- Index for domain-filtered lookups
CREATE INDEX IF NOT EXISTS idx_memories_domain ON memories (domain);

-- Index for confidence-filtered lookups
CREATE INDEX IF NOT EXISTS idx_memories_confidence ON memories (confidence DESC);

-- Workspaces: one per team
CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slack_team_id TEXT UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Conversation contexts — LLM-gated summaries of Slack threads for cross-agent recall
CREATE TABLE IF NOT EXISTS conversation_contexts (
    id              TEXT,                     -- MD5(channel || ':' || thread_ts), deterministic
    channel         TEXT NOT NULL,
    workspace_id    TEXT,                     -- FK to workspaces (tenant isolation)
    thread_ts       TEXT NOT NULL,            -- Slack thread timestamp
    summary         TEXT NOT NULL,            -- LLM-generated summary (max 500 chars)
    embedding       VECTOR(768),             -- Gemini text-embedding-004
    domain          TEXT DEFAULT 'general',
    message_count   INTEGER DEFAULT 0,       -- how many messages informed this summary
    participants    TEXT[] DEFAULT '{}',     -- who was in the conversation
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '30 days'),
    PRIMARY KEY (channel, thread_ts)         -- one summary per thread; upsert by identity
);

-- HNSW index for fast vector similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS idx_contexts_embedding
    ON conversation_contexts
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_contexts_workspace ON conversation_contexts (workspace_id, channel);
CREATE INDEX IF NOT EXISTS idx_contexts_expires ON conversation_contexts (expires_at);

-- Blob-backed raw conversation storage — fallback when LLM gatekeeper fails
-- Reuses the existing observations pattern: session_id = concat('slack:', channel, ':', thread_ts)
CREATE TABLE IF NOT EXISTS conversation_blobs (
    id              TEXT PRIMARY KEY,         -- MD5(channel || ':' || thread_ts)
    channel         TEXT NOT NULL,
    workspace_id    TEXT,
    thread_ts       TEXT NOT NULL,
    messages        JSONB NOT NULL DEFAULT '[]',  -- raw Slack message array
    message_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '14 days')  -- shorter TTL than summaries
);

CREATE INDEX IF NOT EXISTS idx_conversation_blobs_channel ON conversation_blobs (channel, workspace_id);
CREATE INDEX IF NOT EXISTS idx_conversation_blobs_expires ON conversation_blobs (expires_at);

-- Cleanup function: prune expired context and blobs
CREATE OR REPLACE FUNCTION cleanup_expired_contexts()
RETURNS void AS $$
BEGIN
    DELETE FROM conversation_contexts WHERE expires_at < NOW();
    DELETE FROM conversation_blobs WHERE expires_at < NOW();
END;
$$ LANGUAGE plpgsql;

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_contexts_updated_at
    BEFORE UPDATE ON conversation_contexts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
