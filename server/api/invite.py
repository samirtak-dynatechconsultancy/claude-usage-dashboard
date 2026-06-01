"""POST /api/invite — admin-only: allowlist a new dashboard viewer + send
                     them a magic-link email.

Body:
  {"email": "teammate@company.com", "role": "viewer" | "admin"}

Pipeline:
  1. Verify the caller is signed in AND is an admin (role='admin' on
     dashboard_users).
  2. Validate the invitee email + role.
  3. Upsert the invitee into dashboard_users.
  4. Trigger Supabase to send them a magic-link sign-in email via
     auth.sign_in_with_otp. shouldCreateUser=true so brand-new emails
     get an account auto-provisioned at click time.

If the upsert succeeds but the email fails (e.g. SMTP misconfig), the
endpoint still returns success-with-warning -- the allowlist row is
written, so the invitee can be re-invited manually.

Auth: Authorization: Bearer <Supabase JWT>. role must be 'admin'.
"""

import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json, read_json
from lib.supabase_client import service_client


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
VALID_ROLES = {"viewer", "admin"}


def _detect_site_url(request_host_header):
    """Pick the redirect URL the magic-link should land on after click.

    Priority: explicit SITE_URL env var > the request's Host header > nothing
    (in which case Supabase falls back to its project Site URL setting).
    """
    explicit = os.environ.get("SITE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"
    host = (request_host_header or "").strip()
    if host:
        # Use https unless explicitly localhost
        scheme = "http" if host.startswith("localhost") or host.startswith("127.0.0.1") else "https"
        return f"{scheme}://{host}/"
    return None


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

        if not EMAIL_RE.match(invitee_email):
            return write_json(self, 400, {"error": "valid email required"})
        if invitee_role not in VALID_ROLES:
            return write_json(self, 400, {
                "error": "role must be 'viewer' or 'admin'",
                "received": invitee_role,
            })

        sb = service_client()

        # ── 1. Allowlist ────────────────────────────────────────────────────
        try:
            sb.table("dashboard_users").upsert(
                {"email": invitee_email, "role": invitee_role},
                on_conflict="email",
            ).execute()
        except Exception as e:
            return write_json(self, 500, {"error": f"failed to update allowlist: {e}"})

        # ── 2. Send magic-link sign-in email ───────────────────────────────
        redirect_to = _detect_site_url(self.headers.get("Host"))
        email_warning = None
        try:
            opts = {"should_create_user": True}
            if redirect_to:
                opts["email_redirect_to"] = redirect_to
            sb.auth.sign_in_with_otp({"email": invitee_email, "options": opts})
        except Exception as e:
            # Common cause: Supabase Auth -> Providers -> Email "Enable signups"
            # is OFF, or there's a per-project send-rate limit. We've already
            # written the allowlist row, so this isn't fatal.
            email_warning = str(e)

        return write_json(self, 200, {
            "ok":            True,
            "email":         invitee_email,
            "role":          invitee_role,
            "allowlisted":   True,
            "email_sent":    email_warning is None,
            "email_warning": email_warning,
            "invited_by":    caller_email,
        })
