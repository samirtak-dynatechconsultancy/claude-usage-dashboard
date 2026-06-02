-- ─────────────────────────────────────────────────────────────────────────────
-- 0006_rdp_support.sql
--
-- Schema prep for RDP / shared-OS-user scenarios where one physical machine
-- hosts many human users.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. machines: same box, one row per user ──────────────────────────────
-- Keep the user_id FK so the user<->machine relationship stays explicit
-- (one user can have many machines; one machine row belongs to one user).
-- For RDP hosts where many users share one physical box, the same
-- machine_fp now appears across multiple rows (one per user_id) -- the
-- UNIQUE constraint moves from (machine_fp) to (machine_fp, user_id).
--
-- Idempotent: handles three states cleanly --
--   a) Fresh schema:        user_id present, single-col UNIQUE
--   b) Old build of 0006:   user_id dropped, single-col UNIQUE on machine_fp
--   c) Already-on-target:   user_id present, composite UNIQUE
-- Re-running this migration converges all three to (c).

-- Restore user_id if a previous build of 0006 dropped it.
ALTER TABLE machines ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;

-- Back-fill user_id for any rows that are still NULL (would only happen
-- if state (b) had pushes between the drop and now). Use the user_id of
-- the earliest session on this machine as the "owning" user.
UPDATE machines m
SET user_id = COALESCE(m.user_id, (
    SELECT s.user_id FROM sessions s
    WHERE s.machine_id = m.id
    ORDER BY s.first_timestamp NULLS LAST
    LIMIT 1
))
WHERE m.user_id IS NULL;

-- After back-fill (or in state (a)/(c)), user_id is NOT NULL.
ALTER TABLE machines ALTER COLUMN user_id SET NOT NULL;

-- Replace the single-column UNIQUE(machine_fp) with composite
-- UNIQUE(machine_fp, user_id) so multiple users can share one machine_fp.
ALTER TABLE machines DROP CONSTRAINT IF EXISTS machines_machine_fp_key;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'machines_machine_fp_user_id_key'
  ) THEN
    ALTER TABLE machines
      ADD CONSTRAINT machines_machine_fp_user_id_key UNIQUE (machine_fp, user_id);
  END IF;
END $$;

-- ── 2. users: mark RDP-client pseudo-users ────────────────────────────────
-- When a collector running on an RDP host can't get a real OS username, it
-- sends the source device's CLIENTNAME as the os_username (e.g.
-- "DSPL-LPT-534"). The is_rdp flag lets the dashboard render those rows
-- differently (RDP icon, "via RDP from..." metadata).
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_rdp BOOLEAN NOT NULL DEFAULT FALSE;

-- Optional admin-maintainable display_name + email already exist on users;
-- no change needed -- admins fill those in via the new Manage Users UI.

-- ── 3. sessions: track the RDP client device per session ──────────────────
-- Even when a user's identity comes via CLIENTNAME, it's useful to know
-- *which session was launched from where*. Lets the UI show "RDP from
-- LAPTOP-ALICE" even if the user is mapped to a real email.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS client_machine TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS rdp_session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_sessions_client_machine
    ON sessions(client_machine) WHERE client_machine IS NOT NULL;

-- ── 4. machines: track whether the box is an RDP host ─────────────────────
-- Set to TRUE the first time the server sees a push from this machine_fp
-- carrying is_rdp=true. Stays sticky -- a one-time RDP push marks the box
-- as RDP-capable forever, which is fine.
ALTER TABLE machines ADD COLUMN IF NOT EXISTS is_rdp_host BOOLEAN NOT NULL DEFAULT FALSE;

-- ── 5. Convenience view: who's active right now ───────────────────────────
-- "Currently logged in" via the data we already have: any user with
-- last_seen in the last 30 minutes. Admin UI consumes this directly.
CREATE OR REPLACE VIEW active_users_view AS
SELECT
    u.id,
    u.os_username,
    u.display_name,
    u.email,
    u.is_rdp,
    u.last_seen,
    EXTRACT(EPOCH FROM (now() - u.last_seen))::INTEGER AS seconds_since_seen,
    CASE
        WHEN u.last_seen > now() - INTERVAL '30 minutes' THEN 'active'
        WHEN u.last_seen > now() - INTERVAL '24 hours'   THEN 'recent'
        WHEN u.last_seen > now() - INTERVAL '7 days'     THEN 'idle'
        ELSE 'stale'
    END AS activity
FROM users u;
