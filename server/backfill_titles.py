"""Backfill session titles for existing sessions using Claude Haiku.

Reads the first user message from the messages table for each session
that has no title, calls Azure Foundry Haiku to generate a 3-7 word
title, and saves it.

Requires env vars: AZURE_FOUNDRY_ENDPOINT, AZURE_FOUNDRY_API_KEY
Optional: AZURE_FOUNDRY_TITLE_MODEL (default: claude-haiku-4-5-20251001)

Usage:
    set AZURE_FOUNDRY_ENDPOINT=https://...
    set AZURE_FOUNDRY_API_KEY=...
    python server/backfill_titles.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import psycopg2
import psycopg2.extras

from lib.title_generator import generate_title


def main():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: Set DATABASE_URL env var")
        sys.exit(1)
    if not os.environ.get("AZURE_FOUNDRY_ENDPOINT") or not os.environ.get("AZURE_FOUNDRY_API_KEY"):
        print("ERROR: Set AZURE_FOUNDRY_ENDPOINT and AZURE_FOUNDRY_API_KEY env vars")
        sys.exit(1)

    conn = psycopg2.connect(db_url, connect_timeout=30)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Find sessions without titles that have at least one user message
    cur.execute("""
        SELECT s.id, s.session_uuid, s.project_name,
               (SELECT m.text_content FROM messages m
                WHERE m.session_id = s.id AND m.role = 'user'
                ORDER BY m.timestamp ASC LIMIT 1) AS first_msg
        FROM sessions s
        WHERE s.title IS NULL
        ORDER BY s.last_timestamp DESC
    """)
    rows = cur.fetchall()

    total = len(rows)
    print(f"Found {total} sessions without titles")

    generated = 0
    skipped = 0
    failed = 0

    for i, row in enumerate(rows, 1):
        first_msg = (row["first_msg"] or "").strip()
        if not first_msg:
            skipped += 1
            print(f"  [{i}/{total}] {row['session_uuid'][:8]} — no user message, skipping")
            continue

        title, model = generate_title(first_msg)
        if title:
            cur.execute(
                "UPDATE sessions SET title = %s WHERE id = %s",
                (title, row["id"])
            )
            generated += 1
            print(f"  [{i}/{total}] {row['session_uuid'][:8]} — \"{title}\"")
        else:
            failed += 1
            print(f"  [{i}/{total}] {row['session_uuid'][:8]} — FAILED")

        # Small delay to avoid rate limiting
        time.sleep(0.3)

    print(f"\nDone: {generated} generated, {skipped} skipped (no messages), {failed} failed")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
