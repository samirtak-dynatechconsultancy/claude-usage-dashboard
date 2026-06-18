"""POST /api/backfill-titles — generate titles for sessions that don't have one.

Admin-only. Queries sessions without titles, fetches their first user
message, and calls Azure Foundry to generate a 3-7 word title.

Runs synchronously up to a cap (default 50) to stay within Vercel's
function timeout. Call repeatedly until 0 remain.

Query params:
  ?limit=N  — max sessions to process per call (default 50)
"""

import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json
from lib.supabase_client import service_client
from lib.title_generator import generate_title


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            return self._handle()
        except Exception as exc:
            return write_json(self, 500, {"error": f"Server error: {exc}"})

    def _handle(self):
        ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})
        if role != "admin":
            return write_json(self, 403, {"error": "admin only"})

        qs = parse_qs(urlparse(self.path).query)
        limit = min(int((qs.get("limit") or ["50"])[0]), 200)

        endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT")
        api_key = os.environ.get("AZURE_FOUNDRY_API_KEY")
        if not endpoint or not api_key:
            return write_json(self, 500, {
                "error": "AZURE_FOUNDRY_ENDPOINT and AZURE_FOUNDRY_API_KEY env vars not set"
            })

        sb = service_client()

        # Find sessions without titles, ordered by most recent first.
        rows = (
            sb.table("sessions")
            .select("id, session_uuid")
            .is_("title", "null")
            .order("last_timestamp", desc=True)
            .limit(limit)
            .execute()
        ).data or []

        total_untitled = len(rows)
        generated = 0
        failed = 0
        skipped = 0
        results = []

        for row in rows:
            sid = row["id"]
            suuid = row["session_uuid"]

            # Get first user message for this session.
            msg_row = (
                sb.table("messages")
                .select("text_content")
                .eq("session_id", sid)
                .eq("role", "user")
                .order("timestamp")
                .limit(1)
                .execute()
            ).data

            text = (msg_row[0]["text_content"] or "").strip() if msg_row else ""
            if not text:
                skipped += 1
                results.append({"session": str(suuid)[:8], "status": "skipped", "reason": "no user message"})
                continue

            title, model = generate_title(text)
            if title:
                sb.table("sessions").update({"title": title}).eq("id", sid).execute()
                generated += 1
                results.append({"session": str(suuid)[:8], "status": "ok", "title": title})
            else:
                failed += 1
                results.append({"session": str(suuid)[:8], "status": "failed"})

        # Count remaining untitled sessions.
        remaining_resp = (
            sb.table("sessions")
            .select("id", count="exact")
            .is_("title", "null")
            .execute()
        )
        remaining = remaining_resp.count if remaining_resp.count is not None else "?"

        return write_json(self, 200, {
            "ok": True,
            "processed": total_untitled,
            "generated": generated,
            "skipped": skipped,
            "failed": failed,
            "remaining": remaining,
            "results": results,
        })
