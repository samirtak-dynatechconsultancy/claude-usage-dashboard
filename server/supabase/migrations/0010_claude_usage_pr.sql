-- Claude Desktop subscription usage tracking table.
-- Stores 5-hour (session) and 7-day (weekly) utilization percentages
-- reported by the collector's `usage` subcommand.

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
