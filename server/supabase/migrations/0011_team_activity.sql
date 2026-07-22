-- Team Activity: latest claude.ai admin-analytics snapshot per organization.
-- The collector (running on an admin's machine, where the cf_clearance cookie
-- is valid) fetches /analytics/activity/users for each configured org and
-- pushes the result here via /api/ingest. The dashboard reads from this table
-- (the Vercel server can't call claude.ai — Cloudflare blocks its IP).
--
-- One row per org, overwritten on each push. `ok=false` + `error` marks an
-- expired/failed cookie so the dashboard can flag it.

create table if not exists public.team_activity (
    org         text primary key,
    org_name    text,
    captured_at timestamptz not null default now(),
    ok          boolean     not null default true,
    error       text,
    members     jsonb       not null default '[]'::jsonb
);
