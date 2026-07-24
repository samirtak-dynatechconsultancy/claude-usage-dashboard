-- Team Activity: store the full member roster (from claude.ai /members) per org
-- so the dashboard can show seat holders who have never been active (no
-- activity rows, no last_active) along with their seat_tier.
--
-- The collector pushes {"kind":"team_roster", org, roster:[{uuid,email,name,
-- role,seat_tier,created_at}]} to /api/ingest; _handle_team_roster upserts it
-- here. data.py merges this roster with the per-day activity aggregates.

alter table public.team_activity_org
    add column if not exists roster jsonb not null default '[]'::jsonb;
