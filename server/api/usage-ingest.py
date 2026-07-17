"""POST /api/usage-ingest — receive Claude Desktop subscription usage data.

Authenticated by X-Ingest-Token (same as /api/ingest). Inserts a row into
the claude_usage_pr table with the 5-hour and 7-day utilization percentages.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_ingest_token
from lib.http import write_json, read_json
from lib.supabase_client import service_client

TABLE = "claude_usage_pr"


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            return self._handle()
        except Exception as exc:
            return write_json(self, 500, {"error": f"Server error: {exc}"})

    def _handle(self):
        if not verify_ingest_token(self.headers.get("X-Ingest-Token")):
            return write_json(self, 401, {"error": "invalid ingest token"})

        body, err = read_json(self, max_bytes=64 * 1024)
        if err:
            return write_json(self, err[0], err[1])

        row = {
            "email":                body.get("email"),
            "org_id":               body.get("org_id"),
            "session_pct":          body.get("session_pct"),
            "weekly_pct":           body.get("weekly_pct"),
            "five_hour_resets_at":  body.get("five_hour_resets_at"),
            "seven_day_resets_at":  body.get("seven_day_resets_at"),
            "host":                 body.get("host"),
            "os_user":              body.get("os_user"),
        }

        sb = service_client()
        sb.table(TABLE).insert(row).execute()

        return write_json(self, 200, {"ok": True})
