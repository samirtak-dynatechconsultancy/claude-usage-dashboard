"""Cached Supabase client for serverless functions.

Vercel reuses warm function containers, so we cache the client at module scope.
A cold start pays the import + connect cost once; subsequent invocations on
the same container reuse the instance.
"""

import os
from supabase import create_client, Client

_service_client: Client = None
_anon_client: Client = None


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in Vercel → Project Settings → Environment Variables."
        )
    return val


def service_client() -> Client:
    """Server-side client with the service_role key. Bypasses RLS.

    Use this for ingest, admin reads, and storage operations. Never expose
    the underlying key to the browser.
    """
    global _service_client
    if _service_client is None:
        _service_client = create_client(
            _require_env("SUPABASE_URL"),
            _require_env("SUPABASE_SERVICE_ROLE_KEY"),
        )
    return _service_client


def anon_client() -> Client:
    """Client with the public anon key. Subject to Row-Level Security.

    Used only for verifying user JWTs from the dashboard browser.
    """
    global _anon_client
    if _anon_client is None:
        _anon_client = create_client(
            _require_env("SUPABASE_URL"),
            _require_env("SUPABASE_ANON_KEY"),
        )
    return _anon_client


def storage_bucket() -> str:
    return os.environ.get("STORAGE_BUCKET", "claude-raw")
