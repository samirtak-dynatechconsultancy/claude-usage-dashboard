"""Run the Azure PostgreSQL migration from Python.

Usage:
    python run_migration.py "postgresql://dtadmin:PASSWORD@HOST:5432/claude_usage_db?sslmode=require"
"""

import sys
import psycopg2

SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS dashboard_users (
    email           TEXT PRIMARY KEY,
    role            TEXT NOT NULL DEFAULT 'viewer',
    password_hash   TEXT,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO dashboard_users (email, role, password_hash)
VALUES (
    'samir.tak@dynatechconsultancy.com',
    'admin',
    '494a715f7e9b4071aca61bac42ca858a309524e5864f0920030862a4ae7589be'
) ON CONFLICT (email) DO NOTHING;

CREATE TABLE IF NOT EXISTS machine_aliases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hostname)
);

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

CREATE OR REPLACE VIEW active_users_view AS
SELECT
    u.id, u.os_username, u.display_name, u.email, u.is_rdp, u.last_seen,
    EXTRACT(EPOCH FROM (now() - u.last_seen))::INTEGER AS seconds_since_seen,
    CASE
        WHEN u.last_seen > now() - INTERVAL '30 minutes' THEN 'active'
        WHEN u.last_seen > now() - INTERVAL '24 hours'   THEN 'recent'
        WHEN u.last_seen > now() - INTERVAL '7 days'     THEN 'idle'
        ELSE 'stale'
    END AS activity
FROM users u;
"""

FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION recompute_session_totals(target_session_id UUID)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    chosen_model TEXT;
BEGIN
    SELECT model INTO chosen_model
    FROM (
        SELECT model, COUNT(*) AS turn_count,
            CASE
                WHEN LOWER(model) LIKE '%opus%'   THEN 3
                WHEN LOWER(model) LIKE '%sonnet%' THEN 2
                WHEN LOWER(model) LIKE '%haiku%'  THEN 1
                ELSE 0
            END AS priority
        FROM turns
        WHERE session_id = target_session_id AND model IS NOT NULL AND model != ''
        GROUP BY model
    ) AS m ORDER BY priority DESC, turn_count DESC LIMIT 1;

    UPDATE sessions SET
        total_input_tokens     = COALESCE((SELECT SUM(input_tokens)          FROM turns WHERE session_id = target_session_id), 0),
        total_output_tokens    = COALESCE((SELECT SUM(output_tokens)         FROM turns WHERE session_id = target_session_id), 0),
        total_cache_read       = COALESCE((SELECT SUM(cache_read_tokens)     FROM turns WHERE session_id = target_session_id), 0),
        total_cache_creation   = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE session_id = target_session_id), 0),
        turn_count             = COALESCE((SELECT COUNT(*)                   FROM turns WHERE session_id = target_session_id), 0),
        first_timestamp        = COALESCE((SELECT MIN(timestamp)             FROM turns WHERE session_id = target_session_id), first_timestamp),
        last_timestamp         = COALESCE((SELECT MAX(timestamp)             FROM turns WHERE session_id = target_session_id), last_timestamp),
        model                  = COALESCE(chosen_model, model),
        updated_at             = now()
    WHERE id = target_session_id;
END;
$$;
"""

DROP_SQL = """
DROP VIEW IF EXISTS active_users_view CASCADE;
DROP TABLE IF EXISTS user_machine_map CASCADE;
DROP TABLE IF EXISTS machine_aliases CASCADE;
DROP TABLE IF EXISTS processed_files CASCADE;
DROP TABLE IF EXISTS messages CASCADE;
DROP TABLE IF EXISTS turns CASCADE;
DROP TABLE IF EXISTS sessions CASCADE;
DROP TABLE IF EXISTS machines CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS dashboard_users CASCADE;
DROP FUNCTION IF EXISTS recompute_session_totals(UUID);
"""


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_migration.py <DATABASE_URL>")
        print('  e.g. python run_migration.py "postgresql://dtadmin:PASS@host:5432/claude_usage_db?sslmode=require"')
        sys.exit(1)

    url = sys.argv[1]
    # Normalize SQLAlchemy-style URL
    if url.startswith("postgresql+"):
        url = "postgresql://" + url.split("://", 1)[1]
    if "sslmode" not in url and "ssl" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    elif "?ssl=require" in url:
        url = url.replace("?ssl=require", "?sslmode=require")

    print(f"Connecting to: {url.split('@')[1] if '@' in url else url}")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()

    # Drop existing tables from any partial earlier run
    print("\n1/3  Dropping any existing tables from partial runs...")
    cur.execute(DROP_SQL)
    print("     Done.")

    # Create all tables, indexes, views, seed data
    print("2/3  Creating schema (tables, indexes, views, seed data)...")
    cur.execute(SCHEMA_SQL)
    print("     Done.")

    # Create the recompute function
    print("3/3  Creating recompute_session_totals function...")
    cur.execute(FUNCTION_SQL)
    print("     Done.")

    # Verify
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    tables = [r[0] for r in cur.fetchall()]
    print(f"\nTables created: {', '.join(tables)}")

    cur.execute("SELECT email, role FROM dashboard_users")
    admins = cur.fetchall()
    print(f"Dashboard users: {admins}")

    cur.execute("SELECT proname FROM pg_proc WHERE proname = 'recompute_session_totals'")
    fn = cur.fetchone()
    print(f"Function exists: {bool(fn)}")

    cur.close()
    conn.close()
    print("\nMigration complete!")


if __name__ == "__main__":
    main()
