"""Two auth modes used by the API:

  1. Collector ingest — shared X-Ingest-Token header. Cheap and safe enough
     for a trusted team. Any collector with the token can post as any
     (os_username, hostname); the threat model assumes installer distribution
     is controlled.

  2. Dashboard viewer — Supabase Auth JWT in the Authorization header. The
     dashboard signs in via Supabase, gets a JWT, and sends it on every
     /api/data call. We verify the JWT against Supabase, then check the email
     against the dashboard_users allowlist.
"""

import os
import hmac
from typing import Optional, Tuple

from .supabase_client import service_client, anon_client


# ── Collector ingest token ──────────────────────────────────────────────────

def verify_ingest_token(header_value: Optional[str]) -> bool:
    """Constant-time compare against INGEST_TOKEN env var."""
    expected = os.environ.get("INGEST_TOKEN", "")
    if not expected or not header_value:
        return False
    return hmac.compare_digest(expected.encode(), header_value.encode())


# ── Dashboard viewer auth ───────────────────────────────────────────────────

def verify_dashboard_user(authorization_header: Optional[str]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Verify a dashboard request.

    Returns (ok, email, role). ok=False when the JWT is missing/invalid, OR the
    email isn't on the dashboard_users allowlist.
    """
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        return False, None, None
    token = authorization_header.split(" ", 1)[1].strip()
    if not token:
        return False, None, None

    # Verify the JWT against Supabase Auth. gotrue's get_user(token) does this
    # without a round-trip to Auth servers (it validates locally against the
    # project's JWT secret embedded in the anon key's payload).
    try:
        result = anon_client().auth.get_user(token)
        user = getattr(result, "user", None) or (result.get("user") if isinstance(result, dict) else None)
        if user is None:
            return False, None, None
        email = getattr(user, "email", None) or (user.get("email") if isinstance(user, dict) else None)
    except Exception:
        return False, None, None

    if not email:
        return False, None, None

    # Allowlist check — uses service client to bypass RLS on dashboard_users.
    row = (
        service_client()
        .table("dashboard_users")
        .select("email, role")
        .eq("email", email.lower())
        .limit(1)
        .execute()
    )
    data = getattr(row, "data", None) or []
    if not data:
        return False, email, None
    return True, email, data[0].get("role", "viewer")
