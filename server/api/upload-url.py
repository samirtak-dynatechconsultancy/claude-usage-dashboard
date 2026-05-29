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

        # storage3's create_signed_upload_url returns {"url": ..., "token": ...}.
        # The collector PUTs the JSONL body to `url`.
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
