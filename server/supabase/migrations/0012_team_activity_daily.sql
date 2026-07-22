-- Team Activity (daily, per-user) — supersedes 0011's single-snapshot design.
--
-- The collector runs DAILY on an admin's machine (where a valid claude.ai
-- Cookie header lives), fetches /analytics/activity/users for each configured
-- org, and pushes the result here via POST /api/ingest {"kind":"team_activity"}.
-- The Vercel server can't call claude.ai itself (Cloudflare blocks its IP), so
-- all collection happens on the local machine and only the parsed rows land here.
--
-- Two tables:
--   team_activity_daily — one row per (org, snapshot_date, user), full member
--                         object kept in `member` jsonb for forward-compat.
--   team_activity_org   — one row per org: dropdown label + cookie health so the
--                         dashboard can flag an expired/blocked cookie.

create table if not exists public.team_activity_daily (
    id            bigint generated always as identity primary key,
    org           text not null,
    org_name      text,
    snapshot_date date not null,
    captured_at   timestamptz not null default now(),

    -- Stable per-user key within an org (email, else name, else a hash).
    user_key      text not null,
    name          text,
    email         text,
    role          text,

    -- Known metrics from the analytics endpoint (nullable; schema may add more).
    chat_count                 integer,
    message_count              integer,
    projects_created_count     integer,
    projects_used_count        integer,
    code_session_count         integer,
    days_active                integer,
    estimated_spend_us_dollars numeric,
    last_active                text,

    -- The raw member object, so the UI can render fields we didn't column-ize.
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
    ok              boolean not null default true,   -- false => cookie expired/blocked
    error           text,
    member_count    integer
);
