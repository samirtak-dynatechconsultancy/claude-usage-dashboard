"""POST /api/invite — admin-only: add a new dashboard user with password.

Body:
  {"email": "teammate@company.com", "role": "viewer" | "admin",
   "password": "their-password"}

Pipeline:
  1. Verify the caller is signed in AND is an admin.
  2. Validate the invitee email + role + password.
  3. Upsert into dashboard_users with hashed password.

Auth: Authorization: Bearer <JWT>. role must be 'admin'.
"""

import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user, hash_password
from lib.http import write_json, read_json
from lib.supabase_client import service_client


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
VALID_ROLES = {"viewer", "admin"}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        ok, caller_email, caller_role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})
        if caller_role != "admin":
            return write_json(self, 403, {
                "error": "admin role required to invite",
                "your_role": caller_role,
            })

        body, err = read_json(self, max_bytes=4096)
        if err:
            return write_json(self, err[0], err[1])

        invitee_email = (body.get("email") or "").strip().lower()
        invitee_role  = (body.get("role")  or "viewer").strip().lower()
        password      = (body.get("password") or "").strip()

        if not EMAIL_RE.match(invitee_email):
            return write_json(self, 400, {"error": "valid email required"})
        if invitee_role not in VALID_ROLES:
            return write_json(self, 400, {"error": "role must be 'viewer' or 'admin'"})
        if not password or len(password) < 6:
            return write_json(self, 400, {"error": "password must be at least 6 characters"})

        db = service_client()

        try:
            db.table("dashboard_users").upsert(
                {
                    "email":         invitee_email,
                    "role":          invitee_role,
                    "password_hash": hash_password(password),
                },
                on_conflict="email",
            ).execute()
        except Exception as e:
            return write_json(self, 500, {"error": f"failed to update allowlist: {e}"})

        return write_json(self, 200, {
            "ok":          True,
            "email":       invitee_email,
            "role":        invitee_role,
            "invited_by":  caller_email,
        })
