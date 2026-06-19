-- Loom schema. Idempotent: safe to run multiple times.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    rule TEXT NOT NULL CHECK (length(rule) >= 5 AND length(rule) <= 1000),
    example TEXT NOT NULL DEFAULT '',
    confidence INTEGER NOT NULL DEFAULT 5 CHECK (confidence BETWEEN 1 AND 10),
    uses INTEGER NOT NULL DEFAULT 0,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_type TEXT NOT NULL DEFAULT 'user_teach',
    embedding VECTOR(768),
    project TEXT NOT NULL DEFAULT 'default',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conversation_contexts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    topic_index INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 10 AND 500),
    embedding VECTOR(768),
    domain TEXT NOT NULL DEFAULT 'general',
    message_count INTEGER NOT NULL DEFAULT 0,
    participants TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    UNIQUE (workspace_id, channel, thread_ts, topic_index)
);

CREATE TABLE IF NOT EXISTS conversation_blobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    messages JSONB NOT NULL DEFAULT '[]'::jsonb,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '14 days'),
    UNIQUE (workspace_id, channel, thread_ts)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_contexts_embedding ON conversation_contexts
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_memories_domain ON memories(domain);
CREATE INDEX IF NOT EXISTS idx_memories_confidence_desc ON memories(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_contexts_expires_at ON conversation_contexts(expires_at);
CREATE INDEX IF NOT EXISTS idx_blobs_expires_at ON conversation_blobs(expires_at);
CREATE INDEX IF NOT EXISTS idx_contexts_workspace_channel ON conversation_contexts(workspace_id, channel);
CREATE INDEX IF NOT EXISTS idx_blobs_workspace_channel ON conversation_blobs(workspace_id, channel);

CREATE INDEX IF NOT EXISTS idx_memories_text_search ON memories
    USING gin (to_tsvector('english', rule || ' ' || example || ' ' || domain || ' ' || rule_type));

CREATE INDEX IF NOT EXISTS idx_contexts_text_search ON conversation_contexts
    USING gin (to_tsvector('english', summary || ' ' || domain));

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
CREATE TRIGGER trg_memories_updated_at
BEFORE UPDATE ON memories
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_contexts_updated_at ON conversation_contexts;
CREATE TRIGGER trg_contexts_updated_at
BEFORE UPDATE ON conversation_contexts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION cleanup_expired()
RETURNS TABLE(contexts_deleted INTEGER, blobs_deleted INTEGER) AS $$
DECLARE
    c_count INTEGER;
    b_count INTEGER;
BEGIN
    DELETE FROM conversation_contexts WHERE expires_at < NOW();
    GET DIAGNOSTICS c_count = ROW_COUNT;

    DELETE FROM conversation_blobs WHERE expires_at < NOW();
    GET DIAGNOSTICS b_count = ROW_COUNT;

    RETURN QUERY SELECT c_count, b_count;
END;
$$ LANGUAGE plpgsql;
