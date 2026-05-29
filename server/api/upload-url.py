"""POST /api/upload-url — issue a signed Supabase Storage upload URL.

The collector calls this before uploading each raw JSONL file. Returning a
signed URL keeps the service_role key on the server.

Request:
  {"os_username": "samir.tak", "machine_fp": "...", "filename": "foo.jsonl",
   "content_hash": "<sha256>"}

Response:
  {"upload_url": "https://...", "object_path": "raw/<user>/<machine>/<hash>.jsonl",
   "expires_in": 3600}
"""

import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_ingest_token
from lib.http import write_json, read_json
from lib.supabase_client import service_client, storage_bucket


HASH_RE = re.compile(r"^[a-fA-F0-9]{16,64}$")


def _slug(s: str) -> str:
    """Make a string safe for Storage object paths."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", (s or "")[:80])


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not verify_ingest_token(self.headers.get("X-Ingest-Token")):
            return write_json(self, 401, {"error": "invalid ingest token"})

        body, err = read_json(self, max_bytes=8 * 1024)
        if err:
            return write_json(self, err[0], err[1])

        os_username = _slug(body.get("os_username") or "").lower()
        machine_fp  = _slug(body.get("machine_fp") or "")
        content_hash = (body.get("content_hash") or "").strip()
        if not (os_username and machine_fp and content_hash and HASH_RE.match(content_hash)):
            return write_json(self, 400, {
                "error": "os_username, machine_fp, content_hash (hex) are required"
            })

        object_path = f"raw/{os_username}/{machine_fp}/{content_hash}.jsonl"

        sb = service_client()
        bucket = storage_bucket()

        # storage3's create_signed_upload_url() refuses to issue a URL if the
        # object already exists at `object_path` — it raises with a message
        # containing "Duplicate". This happens when a previous push uploaded
        # the file but failed at /api/ingest before saving local state, so
        # the collector retries the same content-hash-keyed path.
        #
        # The path is content-hash addressed, so an existing object at this
        # path has byte-identical content. Safe to delete and re-issue.
        try:
            signed = sb.storage.from_(bucket).create_signed_upload_url(object_path)
        except Exception as e:
            msg = str(e).lower()
            is_duplicate = (
                "duplicate" in msg
                or "already exists" in msg
                or "resource already exists" in msg
                or "409" in msg
            )
            if not is_duplicate:
                raise
            # Best-effort: remove the stale object, then retry. If the remove
            # fails for some reason (race, perms), let the retry surface a
            # clearer error than the original duplicate.
            try:
                sb.storage.from_(bucket).remove([object_path])
            except Exception:
                pass
            signed = sb.storage.from_(bucket).create_signed_upload_url(object_path)

        # supabase-py shapes vary by version; normalize.
        url = signed.get("signed_url") or signed.get("signedUrl") or signed.get("url")
        token = signed.get("token")

        return write_json(self, 200, {
            "upload_url":  url,
            "upload_token": token,
            "object_path":  object_path,
            "bucket":       bucket,
        })
