"""GET /api/config — returns public client config for the browser.

The Supabase URL and anon key are *designed* to be public (RLS is what protects
data, not key secrecy). Serving them here avoids embedding them in HTML at
build time — Vercel runs no build step for static files.
"""

import os
from http.server import BaseHTTPRequestHandler
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from lib.http import write_json


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_ANON_KEY", "")
        if not url or not key:
            return write_json(self, 500, {
                "error": "server missing SUPABASE_URL / SUPABASE_ANON_KEY env vars"
            })
        write_json(self, 200, {
            "supabase_url": url,
            "supabase_anon_key": key,
        })
