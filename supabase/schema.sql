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
