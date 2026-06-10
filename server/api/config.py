"""GET /api/config — returns public client config for the browser.

Now that auth uses server-side JWT (no Supabase), this endpoint just
signals to the frontend that the backend is alive and which auth mode
to use.
"""

import os
from http.server import BaseHTTPRequestHandler
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from lib.http import write_json


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        write_json(self, 200, {
            "auth_mode": "jwt",
            "login_url": "/api/login",
        })
