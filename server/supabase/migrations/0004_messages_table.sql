-- ─────────────────────────────────────────────────────────────────────────────
-- 0004_messages_table.sql
--
-- Postgres-only refactor: stop uploading raw JSONL to Supabase Storage and
-- store parsed message content in a dedicated table instead. This unlocks
-- full-text search across prompts/responses and structured queries on
-- tool use (e.g. "top 10 tools used team-wide").
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── messages ────────────────────────────────────────────────────────────────
-- One row per JSONL record of type 'user' or 'assistant'. Multiple messages
-- can be linked to a single `turn` (the assistant API response): typically
-- one preceding user message + the assistant message itself.
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id         BIGINT REFERENCES turns(id) ON DELETE CASCADE,  -- nullable: user msgs may not bind to a billable turn
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id      UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,

    message_uuid    TEXT,             -- Claude's message.id (assistant only) — dedupe key
    role            TEXT NOT NULL,    -- 'user' | 'assistant'
    timestamp       TIMESTAMPTZ,

    text_content    TEXT,             -- flattened text from all text-blocks, for FTS + previews
    content_blocks  JSONB,            -- full structured content (text + tool_use + tool_result blocks)
    tool_uses       JSONB,            -- denormalized: [{ id, name, input }] for fast SQL filtering
    tool_results    JSONB,            -- denormalized: [{ tool_use_id, content }] (content truncated)

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dedupe: ON CONFLICT (message_uuid) DO NOTHING. NULLs are distinct, so user
-- messages without an id never conflict. Matches the turns table pattern.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_message_uuid ON messages(message_uuid);

-- Lookup patterns:
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_user      ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_turn      ON messages(turn_id) WHERE turn_id IS NOT NULL;

-- Full-text search over prompts/responses:
--   SELECT * FROM messages WHERE to_tsvector('english', text_content) @@ to_tsquery('password');
CREATE INDEX IF NOT EXISTS idx_messages_text_fts
    ON messages USING gin(to_tsvector('english', COALESCE(text_content, '')));

-- Tool-use search:
--   SELECT * FROM messages WHERE tool_uses @> '[{"name": "Bash"}]';
CREATE INDEX IF NOT EXISTS idx_messages_tool_uses ON messages USING gin(tool_uses);

-- ── RLS ────────────────────────────────────────────────────────────────────
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
-- (Same model as turns/sessions: no anon policies = denied by default. Server
-- uses service_role and bypasses RLS.)

-- ── Drop now-unused columns on turns ───────────────────────────────────────
-- These pointed to Supabase Storage objects. With Postgres-only mode they're
-- dead weight. Drop is safe — schema migrations propagate before code does
-- a SELECT, and the new ingest never sets these columns anyway.
ALTER TABLE turns DROP COLUMN IF EXISTS content_path;
ALTER TABLE turns DROP COLUMN IF EXISTS content_offset;

-- ── Drop the now-unused Storage bucket (optional, kept for safety) ─────────
-- Uncomment these once you've confirmed the dashboard works without Storage.
-- DELETE FROM storage.objects WHERE bucket_id = 'claude-raw';
-- DELETE FROM storage.buckets WHERE id = 'claude-raw';
