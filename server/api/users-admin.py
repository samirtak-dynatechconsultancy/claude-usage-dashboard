"""GET  /api/users-admin       -- list all users with activity + RDP info
PATCH /api/users-admin?id=X   -- update display_name / email / is_rdp

Admin-only. Used by the dashboard's "Manage users" modal so the team can
fill in friendly names / emails for CLIENTNAME-based RDP pseudo-users
without round-tripping through Supabase SQL.
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json, read_json
from lib.supabase_client import service_client


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _require_admin(self):
    ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
    if not ok:
        write_json(self, 401, {"error": "not authorized"})
        return None
    if role != "admin":
        write_json(self, 403, {"error": "admin role required", "your_role": role})
        return None
    return email


def _activity_bucket(seconds_since_seen):
    if seconds_since_seen is None:
        return "stale"
    if seconds_since_seen < 30 * 60:        return "active"      # < 30 min
    if seconds_since_seen < 24 * 3600:      return "recent"      # < 24h
    if seconds_since_seen < 7 * 86400:      return "idle"        # < 7d
    return "stale"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if _require_admin(self) is None:
            return

        sb = service_client()
        # Pull users; include their most-recent client_machine per session so
        # the UI can show "via RDP from LAPTOP-ALICE".
        users_resp = (
            sb.table("users")
            .select("id, os_username, display_name, email, is_rdp, first_seen, last_seen")
            .order("last_seen", desc=True)
            .execute()
        )
        users = users_resp.data or []

        # For each user, fetch the most recent client_machine from sessions.
        # Done in one query, indexed by user_id client-side.
        sess_resp = (
            sb.table("sessions")
            .select("user_id, client_machine, last_timestamp")
            .not_.is_("client_machine", "null")
            .order("last_timestamp", desc=True)
            .execute()
        )
        last_client_by_user = {}
        for r in (sess_resp.data or []):
            uid = r["user_id"]
            if uid not in last_client_by_user:
                last_client_by_user[uid] = r["client_machine"]

        now = datetime_now_seconds()
        rows = []
        for u in users:
            last_seen = u.get("last_seen")
            sec = None
            if last_seen:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    sec = int((datetime.now(timezone.utc) - dt).total_seconds())
                except Exception:
                    sec = None
            rows.append({
                "id":                  u["id"],
                "os_username":         u["os_username"],
                "display_name":        u.get("display_name"),
                "email":               u.get("email"),
                "is_rdp":              bool(u.get("is_rdp")),
                "first_seen":          u.get("first_seen"),
                "last_seen":           last_seen,
                "seconds_since_seen":  sec,
                "activity":            _activity_bucket(sec),
                "last_client_machine": last_client_by_user.get(u["id"]),
            })

        write_json(self, 200, {"users": rows, "total": len(rows)})

    def do_PATCH(self):
        if _require_admin(self) is None:
            return

        qs = parse_qs(urlparse(self.path).query)
        user_id = (qs.get("id") or [None])[0]
        if not user_id:
            return write_json(self, 400, {"error": "id query param required"})

        body, err = read_json(self, max_bytes=4096)
        if err:
            return write_json(self, err[0], err[1])

        # Only let admins update these three fields; everything else stays
        # as recorded by the collector.
        patch = {}
        if "display_name" in body:
            dn = (body["display_name"] or "").strip()
            patch["display_name"] = dn or None
        if "email" in body:
            em = (body["email"] or "").strip().lower()
            if em and not EMAIL_RE.match(em):
                return write_json(self, 400, {"error": "invalid email format"})
            patch["email"] = em or None
        if "is_rdp" in body:
            patch["is_rdp"] = bool(body["is_rdp"])

        if not patch:
            return write_json(self, 400, {"error": "nothing to update"})

        sb = service_client()
        try:
            resp = (
                sb.table("users").update(patch).eq("id", user_id)
                .execute()
            )
        except Exception as e:
            return write_json(self, 500, {"error": str(e)})

        if not resp.data:
            return write_json(self, 404, {"error": "user not found"})

        return write_json(self, 200, {"ok": True, "user": resp.data[0]})


def datetime_now_seconds():
    # Convenience helper kept inline so handler doesn't reach into datetime
    # at the top scope every request (Vercel cold-start saving).
    from datetime import datetime, timezone
    return int(datetime.now(timezone.utc).timestamp())
