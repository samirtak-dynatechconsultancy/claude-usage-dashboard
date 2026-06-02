"""Self-service identification endpoint for unmapped RDP clients.

GET /api/identify?client_machine=DSPL-LPT-551
    Returns { "mapped": bool, "os_username": str?, "display_name": str? }
    Used by the tray app to decide whether to show the identify popup.

POST /api/identify
    Body: {
      "client_machine": "DSPL-LPT-551",
      "os_username":    "samir.tak",     # first_name.last_name
      "display_name":   "Samir Tak"?,    # optional pretty name
      "email":          "samir@..."?     # optional
    }
    Creates / updates the mapping so the RDP auto-link in /api/ingest
    immediately picks it up on the next push.

Auth: X-Ingest-Token. Same shared secret the daemon and tray already
have in config.json. No Supabase JWT needed -- this endpoint is hit
from the end-user's machine, not from the dashboard browser.
"""

import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_ingest_token
from lib.http import write_json, read_json
from lib.supabase_client import service_client


# Validation
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$")
EMAIL_RE    = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _stub_machine_fp(client_machine: str) -> str:
    """Synthetic machine_fp for a self-identified client. Uses a "client-"
    prefix so it can never collide with a real collector's MAC-based hash.
    Same input always returns the same hash so re-identifying is idempotent.
    """
    return "client-" + hashlib.sha256(client_machine.encode("utf-8")).hexdigest()[:16]


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if not verify_ingest_token(self.headers.get("X-Ingest-Token")):
            return write_json(self, 401, {"error": "invalid ingest token"})

        qs = parse_qs(urlparse(self.path).query)
        client_machine = (qs.get("client_machine") or [""])[0].strip()
        if not client_machine:
            return write_json(self, 400, {"error": "client_machine required"})

        sb = service_client()

        # Mapping = a machines row with hostname=client_machine.
        m = (
            sb.table("machines").select("user_id")
            .eq("hostname", client_machine).limit(1).execute()
        )
        if not m.data:
            return write_json(self, 200, {"mapped": False, "client_machine": client_machine})

        u = (
            sb.table("users").select("os_username, display_name, email")
            .eq("id", m.data[0]["user_id"]).limit(1).execute()
        )
        if not u.data:
            # Orphaned machine row — treat as unmapped.
            return write_json(self, 200, {"mapped": False, "client_machine": client_machine})

        row = u.data[0]
        return write_json(self, 200, {
            "mapped":         True,
            "client_machine": client_machine,
            "os_username":    row.get("os_username"),
            "display_name":   row.get("display_name"),
            "email":          row.get("email"),
        })

    def do_POST(self):
        if not verify_ingest_token(self.headers.get("X-Ingest-Token")):
            return write_json(self, 401, {"error": "invalid ingest token"})

        body, err = read_json(self, max_bytes=4096)
        if err:
            return write_json(self, err[0], err[1])

        client_machine = (body.get("client_machine") or "").strip()
        os_username    = (body.get("os_username")    or "").strip().lower()
        display_name   = (body.get("display_name")   or "").strip() or None
        email          = (body.get("email")          or "").strip().lower() or None

        # ── Validation ──────────────────────────────────────────────────────
        if not client_machine:
            return write_json(self, 400, {"error": "client_machine required"})
        if not USERNAME_RE.match(os_username):
            return write_json(self, 400, {
                "error": "os_username must be in first_name.last_name format "
                         "(lowercase letters, digits, hyphens, underscores; "
                         "at least one period)"
            })
        if email and not EMAIL_RE.match(email):
            return write_json(self, 400, {"error": "invalid email format"})

        sb = service_client()
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── Block accidental re-identification ──────────────────────────────
        # If this client_machine already has a different user, don't silently
        # overwrite. Admin would need to clean it up first.
        existing = (
            sb.table("machines").select("id, user_id, hostname")
            .eq("hostname", client_machine).limit(1).execute()
        )
        if existing.data:
            existing_user = (
                sb.table("users").select("os_username")
                .eq("id", existing.data[0]["user_id"]).limit(1).execute()
            )
            existing_username = (existing_user.data[0]["os_username"]
                                 if existing_user.data else None)
            if existing_username and existing_username != os_username:
                return write_json(self, 409, {
                    "error":             "this client_machine is already mapped to a different user",
                    "current_user":      existing_username,
                    "tried_to_set":      os_username,
                    "hint":              "ask an admin to update the mapping via the Manage Users dashboard",
                })

        # ── Upsert user ─────────────────────────────────────────────────────
        user_payload = {"os_username": os_username, "last_seen": now_iso}
        if display_name: user_payload["display_name"] = display_name
        if email:        user_payload["email"]        = email
        user_row = (
            sb.table("users")
            .upsert(user_payload, on_conflict="os_username")
            .execute()
        )
        user_id = user_row.data[0]["id"]

        # ── Upsert stub machine to make hostname searchable ─────────────────
        # Uses a synthetic machine_fp prefixed with "client-" so it never
        # collides with a real collector's fingerprint. on_conflict matches
        # the composite UNIQUE(machine_fp, user_id) constraint.
        machine_payload = {
            "user_id":    user_id,
            "hostname":   client_machine,
            "machine_fp": _stub_machine_fp(client_machine),
            "last_seen":  now_iso,
        }
        (
            sb.table("machines")
            .upsert(machine_payload, on_conflict="machine_fp,user_id")
            .execute()
        )

        return write_json(self, 200, {
            "ok":             True,
            "client_machine": client_machine,
            "user_id":        user_id,
            "os_username":    os_username,
        })
