"""GET /api/content?session_uuid=<uuid> — fetch the raw conversation content.

Returns the parsed JSONL records for a session. Server-side proxy keeps
Storage objects private (anon key has no read access via RLS).

Auth: Authorization: Bearer <Supabase JWT>. Email must be on dashboard_users.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json
from lib.supabase_client import service_client, storage_bucket


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

        # Find every distinct content_path referenced by this session's turns.
        sess = (
            sb.table("sessions")
            .select("id")
            .eq("session_uuid", session_uuid)
            .limit(1)
            .execute()
        )
        if not sess.data:
            return write_json(self, 404, {"error": "session not found"})
        sid = sess.data[0]["id"]

        rows = (
            sb.table("turns")
            .select("content_path")
            .eq("session_id", sid)
            .execute()
        )
        paths = sorted({r["content_path"] for r in (rows.data or []) if r.get("content_path")})
        if not paths:
            return write_json(self, 200, {"records": []})

        # Download each file and merge records. JSONL is small relative to
        # the 60s timeout — typical session is < 5 MB.
        records = []
        bucket = storage_bucket()
        for path in paths:
            try:
                blob = sb.storage.from_(bucket).download(path)
                text = blob.decode("utf-8", errors="replace") if isinstance(blob, (bytes, bytearray)) else str(blob)
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("sessionId") == session_uuid:
                        records.append(rec)
            except Exception as e:
                records.append({"_error": str(e), "_path": path})

        return write_json(self, 200, {"records": records, "files": paths})
