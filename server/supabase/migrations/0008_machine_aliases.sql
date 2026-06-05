-- 0008_machine_aliases.sql
-- Machine aliases + user-machine mapping table.
-- Idempotent — safe to re-run.

-- ── 1. Add display_name to machines (for quick alias) ───────────────────
ALTER TABLE machines ADD COLUMN IF NOT EXISTS display_name TEXT;

-- ── 2. machine_aliases: admin-editable friendly names ────────────────────
-- Separate table so aliases survive machine re-creation (e.g. if a machine
-- row is deleted and re-created by the collector after a state reset).
-- Keyed on hostname (not machine_id) so the alias sticks even if the
-- machine_fp changes after a reimage.
CREATE TABLE IF NOT EXISTS machine_aliases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hostname)
);

ALTER TABLE machine_aliases ENABLE ROW LEVEL SECURITY;

-- ── 3. user_machine_map: explicit user ↔ machine assignments ─────────────
-- The machines table already has user_id, but that's set automatically by
-- the collector and can be wrong (RDP pseudo-users). This table lets
-- admins explicitly assign "this human uses these machines" for clean
-- attribution and filtering.
CREATE TABLE IF NOT EXISTS user_machine_map (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id  UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'admin' | 'shared'
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, machine_id)
);

CREATE INDEX IF NOT EXISTS idx_user_machine_map_user
    ON user_machine_map(user_id);
CREATE INDEX IF NOT EXISTS idx_user_machine_map_machine
    ON user_machine_map(machine_id);

ALTER TABLE user_machine_map ENABLE ROW LEVEL SECURITY;
