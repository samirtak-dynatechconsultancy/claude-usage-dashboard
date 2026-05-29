"""GET /api/auth-check — used by the dashboard login screen.

Returns {ok: true, email, role} when the JWT is valid AND the email is on
the dashboard_users allowlist. Returns 401/403 otherwise so the UI can
distinguish "not signed in" from "signed in but not allowed".
"""

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("Authorization")
        if not auth:
            return write_json(self, 401, {"error": "missing Authorization header"})

        ok, email, role = verify_dashboard_user(auth)
        if not ok and email:
            # JWT was valid but email isn't allowlisted — tell the UI clearly.
            return write_json(self, 403, {
                "error": f"{email} is not in the dashboard allowlist",
                "email": email,
            })
        if not ok:
            return write_json(self, 401, {"error": "invalid token"})

        return write_json(self, 200, {"ok": True, "email": email, "role": role})
