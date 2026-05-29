"""Tiny helpers used by every API handler.

Vercel's Python runtime exposes each handler as a BaseHTTPRequestHandler
subclass — there's no framework abstraction, so we build the minimum we need.
"""

import json
from http.server import BaseHTTPRequestHandler


def write_json(handler: BaseHTTPRequestHandler, status: int, body) -> None:
    payload = json.dumps(body, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def read_json(handler: BaseHTTPRequestHandler, max_bytes: int = 4 * 1024 * 1024):
    """Read & parse the JSON request body. Returns (data, error_response).

    Vercel hard-caps request bodies at ~4.5MB on the Hobby/Pro plans, so we
    defensively reject anything over 4 MB before parsing.
    """
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return None, (400, {"error": "empty body"})
    if length > max_bytes:
        return None, (413, {"error": f"body too large ({length} bytes); max is {max_bytes}"})
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, (400, {"error": f"invalid JSON: {e}"})
