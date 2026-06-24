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

-- Compatibility for databases created before the shared memory layer added
-- project-aware rules. CREATE TABLE IF NOT EXISTS does not add missing columns.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS project TEXT NOT NULL DEFAULT 'default';
UPDATE memories SET example = '' WHERE example IS NULL;
UPDATE memories SET sources = '[]'::jsonb WHERE sources IS NULL;
UPDATE memories SET source_type = 'user_teach'
WHERE source_type IS NULL OR source_type = '';
UPDATE memories SET project = 'default' WHERE project IS NULL OR project = '';
ALTER TABLE memories ALTER COLUMN example SET NOT NULL;
ALTER TABLE memories ALTER COLUMN sources SET NOT NULL;
ALTER TABLE memories ALTER COLUMN source_type SET NOT NULL;
ALTER TABLE memories ALTER COLUMN project SET NOT NULL;

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

-- Compatibility for the legacy contexts table, whose primary key was
-- (channel, thread_ts) and which had no topic_index column. Current writes use
-- ON CONFLICT over workspace/channel/thread/topic and must allow multiple topics.
ALTER TABLE conversation_contexts
    ADD COLUMN IF NOT EXISTS topic_index INTEGER DEFAULT 0;
UPDATE conversation_contexts SET workspace_id = '' WHERE workspace_id IS NULL;
UPDATE conversation_contexts SET topic_index = 0 WHERE topic_index IS NULL;
UPDATE conversation_contexts SET domain = 'general'
WHERE domain IS NULL OR domain = '';
UPDATE conversation_contexts
SET id = md5(workspace_id || ':' || channel || ':' || thread_ts || ':' || topic_index)
WHERE id IS NULL OR id = '';
ALTER TABLE conversation_contexts ALTER COLUMN workspace_id SET DEFAULT '';
ALTER TABLE conversation_contexts ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE conversation_contexts ALTER COLUMN topic_index SET DEFAULT 0;
ALTER TABLE conversation_contexts ALTER COLUMN topic_index SET NOT NULL;
ALTER TABLE conversation_contexts ALTER COLUMN domain SET NOT NULL;
ALTER TABLE conversation_contexts ALTER COLUMN id SET NOT NULL;

DO $$
DECLARE
    pkey_name TEXT;
    pkey_columns TEXT[];
BEGIN
    SELECT con.conname, array_agg(att.attname ORDER BY cols.ordinality)
    INTO pkey_name, pkey_columns
    FROM pg_constraint con
    JOIN unnest(con.conkey) WITH ORDINALITY AS cols(attnum, ordinality) ON TRUE
    JOIN pg_attribute att
        ON att.attrelid = con.conrelid AND att.attnum = cols.attnum
    WHERE con.conrelid = 'conversation_contexts'::regclass
      AND con.contype = 'p'
    GROUP BY con.conname
    LIMIT 1;

    IF pkey_name IS NOT NULL AND pkey_columns <> ARRAY['id']::TEXT[] THEN
        EXECUTE format('ALTER TABLE conversation_contexts DROP CONSTRAINT %I', pkey_name);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'conversation_contexts'::regclass
          AND contype = 'p'
    ) THEN
        ALTER TABLE conversation_contexts
            ADD CONSTRAINT conversation_contexts_pkey PRIMARY KEY (id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'conversation_contexts'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid)
              = 'UNIQUE (workspace_id, channel, thread_ts, topic_index)'
    ) THEN
        ALTER TABLE conversation_contexts
            ADD CONSTRAINT conversation_contexts_workspace_channel_thread_topic_unique
            UNIQUE (workspace_id, channel, thread_ts, topic_index);
    END IF;
END $$;

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

-- Compatibility for legacy blob tables that lacked the conflict target used by
-- save_conversation_blob(). Existing NULL workspace IDs are normalized first so
-- uniqueness and future upserts address the same row.
UPDATE conversation_blobs SET workspace_id = '' WHERE workspace_id IS NULL;
UPDATE conversation_blobs SET messages = '[]'::jsonb WHERE messages IS NULL;
UPDATE conversation_blobs SET message_count = 0 WHERE message_count IS NULL;
ALTER TABLE conversation_blobs ALTER COLUMN workspace_id SET DEFAULT '';
ALTER TABLE conversation_blobs ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE conversation_blobs ALTER COLUMN messages SET NOT NULL;
ALTER TABLE conversation_blobs ALTER COLUMN message_count SET NOT NULL;

WITH ranked_blobs AS (
    SELECT
        ctid,
        row_number() OVER (
            PARTITION BY workspace_id, channel, thread_ts
            ORDER BY created_at DESC NULLS LAST, id DESC
        ) AS rn
    FROM conversation_blobs
)
DELETE FROM conversation_blobs b
USING ranked_blobs r
WHERE b.ctid = r.ctid AND r.rn > 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'conversation_blobs'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid)
              = 'UNIQUE (workspace_id, channel, thread_ts)'
    ) THEN
        ALTER TABLE conversation_blobs
            ADD CONSTRAINT conversation_blobs_workspace_channel_thread_unique
            UNIQUE (workspace_id, channel, thread_ts);
    END IF;
END $$;

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
