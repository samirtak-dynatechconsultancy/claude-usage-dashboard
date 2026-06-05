"""Two auth modes used by the API:

  1. Collector ingest — shared X-Ingest-Token header. Cheap and safe enough
     for a trusted team. Any collector with the token can post as any
     (os_username, hostname); the threat model assumes installer distribution
     is controlled.

  2. Dashboard viewer — JWT in the Authorization header, signed with
     JWT_SECRET env var. The login endpoint (/api/login) validates email +
     password against dashboard_users, then issues a JWT. Every /api/data
     call sends this JWT; we verify the signature and check the email
     against the dashboard_users allowlist.
"""

import os
import hmac
import hashlib
import time
from typing import Optional, Tuple

import jwt  # PyJWT

from .supabase_client import service_client


JWT_ALGORITHM = "HS256"
JWT_EXPIRY_S = 86400 * 7   # 7 days


def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        raise RuntimeError("Missing JWT_SECRET environment variable.")
    return secret


# ── Collector ingest token ──────────────────────────────────────────────────

def verify_ingest_token(header_value: Optional[str]) -> bool:
    """Constant-time compare against INGEST_TOKEN env var."""
    expected = os.environ.get("INGEST_TOKEN", "")
    if not expected or not header_value:
        return False
    return hmac.compare_digest(expected.encode(), header_value.encode())


# ── JWT issuance (called by /api/login) ─────────────────────────────────────

def hash_password(password: str) -> str:
    """SHA-256 hash for password storage. Simple but adequate for a
    team-internal tool behind an allowlist."""
    return hashlib.sha256(password.encode()).hexdigest()


def issue_jwt(email: str, role: str) -> str:
    """Create a signed JWT for a dashboard user."""
    payload = {
        "email": email,
        "role":  role,
        "iat":   int(time.time()),
        "exp":   int(time.time()) + JWT_EXPIRY_S,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


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

    # Decode and verify the JWT signature + expiry.
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        email = payload.get("email")
    except jwt.ExpiredSignatureError:
        return False, None, None
    except jwt.InvalidTokenError:
        return False, None, None

    if not email:
        return False, None, None

    # Allowlist check against dashboard_users table.
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
