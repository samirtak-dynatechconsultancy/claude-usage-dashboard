"""GET /api/content?session_uuid=<uuid> — fetch parsed conversation content.

Postgres-only mode: reads from the `messages` table instead of fetching raw
JSONL files from Supabase Storage. Returns records in the same shape the
local dashboard.py / older Storage-based endpoint did, so the existing
dashboard JS modal needs zero changes.

Auth: Authorization: Bearer <Supabase JWT>. Email must be on dashboard_users.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json
from lib.supabase_client import service_client


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})

        qs = parse_qs(urlparse(self.path).query)
        session_uuid = (qs.get("session_uuid") or [None])[0]
        if not session_uuid:
            return write_json(self, 400, {"error": "session_uuid required"})

        sb = service_client()

        # Look up session_id (uuid PK) from the public session_uuid.
        sess = (
            sb.table("sessions").select("id")
            .eq("session_uuid", session_uuid).limit(1).execute()
        )
        if not sess.data:
            return write_json(self, 404, {"error": "session not found"})
        sid = sess.data[0]["id"]

        # Pull every message for the session, ordered by time. A long
        # conversation might have a few hundred messages with large text
        # content (~50KB each). Paginate to be safe.
        records = []
        page = 0
        PAGE_SIZE = 500
        while True:
            chunk = (
                sb.table("messages")
                .select("role, timestamp, text_content, content_blocks, "
                        "tool_uses, tool_results, message_uuid")
                .eq("session_id", sid)
                .order("timestamp")
                .range(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE - 1)
                .execute()
            )
            data = chunk.data or []
            if not data:
                break
            for m in data:
                # Reconstruct the JSONL-style shape the dashboard JS expects:
                #   { type, timestamp, message: { content } }
                records.append({
                    "type":      m["role"],
                    "timestamp": m["timestamp"],
                    "message": {
                        "id":      m.get("message_uuid"),
                        "content": m.get("content_blocks") or [],
                    },
                })
            if len(data) < PAGE_SIZE:
                break
            page += 1

        return write_json(self, 200, {
            "records": records,
            "count":   len(records),
        })
