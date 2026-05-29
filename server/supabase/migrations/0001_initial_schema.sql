-- ─────────────────────────────────────────────────────────────────────────────
-- 0001_initial_schema.sql
--
-- Run this in the Supabase SQL editor (Project → SQL → New query) once after
-- creating the project. Idempotent — safe to re-run.
--
-- Creates:
--   • Multi-tenant schema (users, machines, sessions, turns)
--   • Storage bucket for raw JSONL files
--   • Row-Level Security policies (server uses service_role and bypasses these;
--     dashboard JS uses anon and is gated by dashboard_users allowlist)
--   • Indexes that match the dashboard's query patterns
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Extensions ───────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()

-- ── users ────────────────────────────────────────────────────────────────────
-- One row per (OS username + email if known). The collector identifies a user
-- by the OS login name from the machine; an admin can fill in `display_name`
-- and `email` later from the dashboard to make reports readable.
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    os_username     TEXT NOT NULL,                    -- e.g. "samir.tak"
    display_name    TEXT,                             -- editable in dashboard
    email           TEXT,                             -- editable in dashboard
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (os_username)
);

-- ── machines ─────────────────────────────────────────────────────────────────
-- One person can have multiple laptops; we keep them separate so it's clear
-- when a machine goes silent (employee left, machine reimaged, etc.).
CREATE TABLE IF NOT EXISTS machines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hostname        TEXT NOT NULL,                    -- e.g. "SAMIR-DESKTOP"
    os              TEXT,                             -- "Windows 11", "macOS 14", ...
    machine_fp      TEXT NOT NULL,                    -- stable fingerprint, see collector
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (machine_fp)
);

CREATE INDEX IF NOT EXISTS idx_machines_user ON machines(user_id);

-- ── sessions ─────────────────────────────────────────────────────────────────
-- Mirrors the local scanner's `sessions` table, plus user/machine attribution.
-- session_uuid is the Claude Code sessionId from the JSONL — globally unique.
CREATE TABLE IF NOT EXISTS sessions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_uuid            TEXT NOT NULL,                   -- Claude Code sessionId
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id              UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    project_name            TEXT,
    git_branch              TEXT,
    first_timestamp         TIMESTAMPTZ,
    last_timestamp          TIMESTAMPTZ,
    model                   TEXT,                            -- primary model (opus > sonnet > haiku)
    turn_count              INTEGER NOT NULL DEFAULT 0,
    total_input_tokens      BIGINT NOT NULL DEFAULT 0,
    total_output_tokens     BIGINT NOT NULL DEFAULT 0,
    total_cache_read        BIGINT NOT NULL DEFAULT 0,
    total_cache_creation    BIGINT NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_uuid)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user      ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_machine   ON sessions(machine_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts   ON sessions(last_timestamp);

-- ── turns ────────────────────────────────────────────────────────────────────
-- One row per assistant API response. `message_id` is the dedupe key — Claude
-- Code emits multiple JSONL records per response while streaming; only the
-- last has the final usage tallies. The collector strips duplicates client-
-- side, but the UNIQUE constraint here is the belt-and-suspenders.
--
-- content_path points to the raw JSONL chunk in Supabase Storage — fetched
-- by the dashboard when the user drills into a conversation.
CREATE TABLE IF NOT EXISTS turns (
    id                      BIGSERIAL PRIMARY KEY,
    session_id              UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id              UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    message_id              TEXT,                            -- dedupe key, may be NULL
    timestamp               TIMESTAMPTZ,
    model                   TEXT,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    tool_name               TEXT,
    cwd                     TEXT,
    content_path            TEXT,                            -- Supabase Storage object key
    content_offset          INTEGER,                         -- byte offset within file (optional)
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Conditional unique index: dedupe only when message_id is present.
-- Matches the scanner.py pattern; lets INSERT ... ON CONFLICT DO NOTHING work.
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
    ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != '';

CREATE INDEX IF NOT EXISTS idx_turns_session   ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_user_ts   ON turns(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
CREATE INDEX IF NOT EXISTS idx_turns_model     ON turns(model);

-- ── processed_files ──────────────────────────────────────────────────────────
-- Server-side mirror of the collector's local state, used for diagnostics and
-- "show me which machines haven't uploaded in a while" queries.
CREATE TABLE IF NOT EXISTS processed_files (
    id              BIGSERIAL PRIMARY KEY,
    machine_id      UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,                    -- path on the user's machine
    mtime           DOUBLE PRECISION NOT NULL,
    lines           INTEGER NOT NULL,
    content_path    TEXT,                             -- Storage key for the latest upload
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (machine_id, path)
);

-- ── dashboard_users ──────────────────────────────────────────────────────────
-- Allowlist of emails that may sign in to view the dashboard. Anyone not on
-- this list gets a 403 even if they create a Supabase Auth account.
CREATE TABLE IF NOT EXISTS dashboard_users (
    email           TEXT PRIMARY KEY,
    role            TEXT NOT NULL DEFAULT 'viewer',   -- 'viewer' | 'admin'
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the first admin — REPLACE with your email before running.
INSERT INTO dashboard_users (email, role)
VALUES ('samir.tak@dynatechconsultancy.com', 'admin')
ON CONFLICT (email) DO NOTHING;

-- ── Storage bucket for raw JSONL ─────────────────────────────────────────────
-- Create the bucket if it doesn't exist. Path convention:
--   raw/{user_id}/{machine_id}/{sha256-of-file}.jsonl
INSERT INTO storage.buckets (id, name, public)
VALUES ('claude-raw', 'claude-raw', false)
ON CONFLICT (id) DO NOTHING;

-- ── Row-Level Security ───────────────────────────────────────────────────────
-- The server uses the service_role key, which bypasses RLS — these policies
-- are for the browser (anon key) only. We deny all anon access; the dashboard
-- always goes through `/api/data` which auth-checks via Supabase JWT.
ALTER TABLE users           ENABLE ROW LEVEL SECURITY;
ALTER TABLE machines        ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE turns           ENABLE ROW LEVEL SECURITY;
ALTER TABLE processed_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_users ENABLE ROW LEVEL SECURITY;

-- No anon SELECT/INSERT/UPDATE/DELETE policies = denied by default. Good.

-- Storage bucket policy: deny anon, allow service_role implicitly.
-- (Supabase Storage uses the same `storage.objects` RLS table.)
DROP POLICY IF EXISTS "claude-raw deny anon" ON storage.objects;
CREATE POLICY "claude-raw deny anon"
    ON storage.objects FOR ALL
    TO anon
    USING (false);
