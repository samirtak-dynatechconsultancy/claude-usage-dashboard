-- ─────────────────────────────────────────────────────────────────────────────
-- 0005_ai_summary.sql
--
-- Cache AI-generated session summaries on the sessions row, so a click on
-- "Generate summary" in the dashboard only calls Azure Foundry once per
-- session, ever. Re-opens are instant from the DB.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ai_summary    TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ai_summary_at TIMESTAMPTZ;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ai_summary_model TEXT;  -- which model generated it

-- Index isn't strictly needed (lookups happen by id), but helps queries
-- like "show me all summarized sessions in the last week".
CREATE INDEX IF NOT EXISTS idx_sessions_ai_summary_at
    ON sessions(ai_summary_at) WHERE ai_summary IS NOT NULL;
