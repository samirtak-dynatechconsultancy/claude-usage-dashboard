-- ═══════════════════════════════════════════════════════════════════════════
-- Combined schema for Azure Database for PostgreSQL (fresh install)
-- Run this ONCE against the claude_usage_db database.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── users ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    os_username     TEXT NOT NULL,
    display_name    TEXT,
    email           TEXT,
    is_rdp          BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (os_username)
);

-- ── machines ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS machines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hostname        TEXT NOT NULL,
    os              TEXT,
    machine_fp      TEXT NOT NULL,
    display_name    TEXT,
    is_rdp_host     BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (machine_fp, user_id)
);

CREATE INDEX IF NOT EXISTS idx_machines_user ON machines(user_id);

-- ── sessions ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_uuid            TEXT NOT NULL,
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id              UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    project_name            TEXT,
    git_branch              TEXT,
    first_timestamp         TIMESTAMPTZ,
    last_timestamp          TIMESTAMPTZ,
    model                   TEXT,
    turn_count              INTEGER NOT NULL DEFAULT 0,
    total_input_tokens      BIGINT NOT NULL DEFAULT 0,
    total_output_tokens     BIGINT NOT NULL DEFAULT 0,
    total_cache_read        BIGINT NOT NULL DEFAULT 0,
    total_cache_creation    BIGINT NOT NULL DEFAULT 0,
    client_machine          TEXT,
    rdp_session_id          TEXT,
    title                   TEXT,
    ai_summary              TEXT,
    ai_summary_at           TIMESTAMPTZ,
    ai_summary_model        TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_uuid)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user      ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_machine   ON sessions(machine_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts   ON sessions(last_timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_client_machine ON sessions(client_machine) WHERE client_machine IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_ai_summary_at  ON sessions(ai_summary_at) WHERE ai_summary IS NOT NULL;

-- ── turns ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS turns (
    id                      BIGSERIAL PRIMARY KEY,
    session_id              UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id              UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    message_id              TEXT,
    timestamp               TIMESTAMPTZ,
    model                   TEXT,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    tool_name               TEXT,
    cwd                     TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
    ON turns(message_id);
CREATE INDEX IF NOT EXISTS idx_turns_session   ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_user_ts   ON turns(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
CREATE INDEX IF NOT EXISTS idx_turns_model     ON turns(model);

-- ── messages ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id         BIGINT REFERENCES turns(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id      UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    message_uuid    TEXT,
    role            TEXT NOT NULL,
    timestamp       TIMESTAMPTZ,
    text_content    TEXT,
    content_blocks  JSONB,
    tool_uses       JSONB,
    tool_results    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_message_uuid ON messages(message_uuid);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_user      ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_turn      ON messages(turn_id) WHERE turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_text_fts
    ON messages USING gin(to_tsvector('english', COALESCE(text_content, '')));
CREATE INDEX IF NOT EXISTS idx_messages_tool_uses ON messages USING gin(tool_uses);

-- ── processed_files ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS processed_files (
    id              BIGSERIAL PRIMARY KEY,
    machine_id      UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    mtime           DOUBLE PRECISION NOT NULL,
    lines           INTEGER NOT NULL,
    content_path    TEXT,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (machine_id, path)
);

-- ── dashboard_users ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dashboard_users (
    email           TEXT PRIMARY KEY,
    role            TEXT NOT NULL DEFAULT 'viewer',
    password_hash   TEXT,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed admin (password: changeme123)
INSERT INTO dashboard_users (email, role, password_hash)
VALUES (
    'samir.tak@dynatechconsultancy.com',
    'admin',
    '494a715f7e9b4071aca61bac42ca858a309524e5864f0920030862a4ae7589be'
) ON CONFLICT (email) DO NOTHING;

-- ── machine_aliases ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS machine_aliases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hostname)
);

-- ── user_machine_map ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_machine_map (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id  UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'user',
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, machine_id)
);

CREATE INDEX IF NOT EXISTS idx_user_machine_map_user    ON user_machine_map(user_id);
CREATE INDEX IF NOT EXISTS idx_user_machine_map_machine ON user_machine_map(machine_id);

-- ── active_users_view ───────────────────────────────────────────────────
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

-- Claude Desktop subscription usage (5h/7d %) from the collector `usage` cmd.
-- Added after usage pushes silently 500'd: table was missing on Azure and the
-- db.py adapter had no insert(). See ingest.py _handle_usage.
create table if not exists public.claude_usage_pr (
    id                  bigint generated always as identity primary key,
    captured_at         timestamptz not null default now(),
    email               text,
    org_id              text,
    session_pct         numeric,
    weekly_pct          numeric,
    five_hour_resets_at timestamptz,
    seven_day_resets_at timestamptz,
    host                text,
    os_user             text
);

-- ── Team Activity (daily, per-user) ─────────────────────────────────────────
-- claude.ai admin analytics collected DAILY on an admin's machine and pushed
-- via /api/ingest {"kind":"team_activity"}. See migration 0012 + ingest.py
-- _handle_team_activity. team_activity_org powers the dashboard org dropdown and
-- the "cookie expired" flag; team_activity_daily holds per-user daily rows.
create table if not exists public.team_activity_daily (
    id            bigint generated always as identity primary key,
    org           text not null,
    org_name      text,
    snapshot_date date not null,
    captured_at   timestamptz not null default now(),
    user_key      text not null,
    name          text,
    email         text,
    role          text,
    chat_count                 integer,
    message_count              integer,
    projects_created_count     integer,
    projects_used_count        integer,
    code_session_count         integer,
    days_active                integer,
    estimated_spend_us_dollars numeric,
    last_active                text,
    member        jsonb not null default '{}'::jsonb,
    unique (org, snapshot_date, user_key)
);
create index if not exists team_activity_daily_org_date_idx
    on public.team_activity_daily (org, snapshot_date desc);
create index if not exists team_activity_daily_date_idx
    on public.team_activity_daily (snapshot_date desc);

create table if not exists public.team_activity_org (
    org             text primary key,
    org_name        text,
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    ok              boolean not null default true,
    error           text,
    member_count    integer
);
