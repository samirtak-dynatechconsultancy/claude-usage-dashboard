"""POST /api/login — authenticate dashboard user and return JWT.

Request body: { "email": "...", "password": "..." }
Response:     { "token": "...", "email": "...", "role": "..." }

Replaces Supabase Auth magic-link flow with simple email + password.
Passwords are SHA-256 hashed and stored in dashboard_users.password_hash.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import hash_password, issue_jwt
from lib.http import write_json, read_json
from lib.supabase_client import service_client


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body, err = read_json(self, max_bytes=4096)
            if err:
                return write_json(self, err[0], err[1])

            email = (body.get("email") or "").strip().lower()
            password = (body.get("password") or "").strip()

            if not email or not password:
                return write_json(self, 400, {"error": "email and password required"})

            db = service_client()

            # Look up the user in dashboard_users.
            row = (
                db.table("dashboard_users")
                .select("email, role, password_hash")
                .eq("email", email)
                .limit(1)
                .execute()
            )
            if not row.data:
                return write_json(self, 401, {"error": "invalid email or password"})

            user = row.data[0]
            stored_hash = user.get("password_hash") or ""

            # Verify password.
            if not stored_hash or hash_password(password) != stored_hash:
                return write_json(self, 401, {"error": "invalid email or password"})

            role = user.get("role", "viewer")
            token = issue_jwt(email, role)

            return write_json(self, 200, {
                "token": token,
                "email": email,
                "role":  role,
            })
        except Exception as exc:
            return write_json(self, 500, {"error": f"Server error: {exc}"})
