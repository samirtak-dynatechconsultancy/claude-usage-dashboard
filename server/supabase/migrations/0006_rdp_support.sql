-- ─────────────────────────────────────────────────────────────────────────────
-- 0006_rdp_support.sql
--
-- Schema prep for RDP / shared-OS-user scenarios where one physical machine
-- hosts many human users.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. machines: drop the per-machine "owner" coupling ────────────────────
-- Previously the machines table had a NOT NULL user_id FK, which meant one
-- box = one user. An RDP host hosts many users, so the relationship has to
-- live in sessions (which already links user + machine) instead.
ALTER TABLE machines DROP CONSTRAINT IF EXISTS machines_user_id_fkey;
ALTER TABLE machines DROP COLUMN IF EXISTS user_id;

-- machine_fp stays UNIQUE — same box still has exactly one row.

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
