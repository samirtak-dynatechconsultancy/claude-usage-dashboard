"""GET /api/team-activity — proxy the claude.ai Team admin analytics endpoint.

Fetches per-user activity (chats, messages, days active, spend, ...) from
  https://claude.ai/api/organizations/{ORG}/analytics/activity/users
using an admin session cookie stored server-side, and returns it to the
dashboard. The raw session cookie never reaches the browser.

Query params (passed through, with defaults):
  page (1), page_size (50), start_date (YYYY-MM-DD), sort (chats), order (desc)

Server env required:
  CLAUDE_SESSION_KEY   an org-admin's claude.ai sessionKey (sk-ant-sid...)
  CLAUDE_ANALYTICS_ORG the organization UUID

Auth: Authorization: Bearer <dashboard JWT> (same as the other endpoints).
On upstream error it returns 200 with an {error, ...} body so the page can
show the error (e.g. a 401 = the session key expired and needs refreshing).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json

CLAUDE_BASE = "https://claude.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _first(qs, key, default):
    v = qs.get(key)
    return v[0] if v else default


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            return self._handle()
        except Exception as exc:
            return write_json(self, 500, {"error": f"Server error: {exc}"})

    def _handle(self):
        ok, _email, _role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})

        # Prefer the session key/org the viewer supplies from their own logged-in
        # claude.ai (sent as headers, never stored server-side). Fall back to the
        # server env vars if configured. The browser can't call claude.ai
        # directly (cross-origin + httpOnly cookie), so it hands the key to this
        # proxy, which makes the call.
        session_key = (self.headers.get("X-Claude-Session-Key")
                       or os.environ.get("CLAUDE_SESSION_KEY", "")).strip()
        org = (self.headers.get("X-Claude-Org")
               or _first(parse_qs(urlparse(self.path).query), "org", "")
               or os.environ.get("CLAUDE_ANALYTICS_ORG", "")).strip()
        if not session_key or not org:
            return write_json(self, 200, {
                "error": "Team Activity is not configured",
                "detail": "Set CLAUDE_SESSION_KEY (an org-admin claude.ai "
                          "sessionKey, sk-ant-sid...) and CLAUDE_ANALYTICS_ORG "
                          "(the org UUID) in the Vercel environment, then "
                          "redeploy. Refresh CLAUDE_SESSION_KEY if this later "
                          "shows a 401 (the key expired).",
            })

        qs = parse_qs(urlparse(self.path).query)
        params = {
            "page":      _first(qs, "page", "1"),
            "page_size": _first(qs, "page_size", "50"),
            "sort":      _first(qs, "sort", "chats"),
            "order":     _first(qs, "order", "desc"),
        }
        start_date = _first(qs, "start_date", "")
        if start_date:
            params["start_date"] = start_date

        url = (f"{CLAUDE_BASE}/api/organizations/{org}"
               f"/analytics/activity/users?{urlencode(params)}")
        req = urlrequest.Request(url, headers={
            "Cookie": f"sessionKey={session_key}",
            "User-Agent": UA,
            "Accept": "application/json",
        })

        try:
            with urlrequest.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")[:500]
            except Exception:
                pass
            hint = ("The session key likely expired — refresh CLAUDE_SESSION_KEY."
                    if e.code in (401, 403) else "")
            return write_json(self, 200, {
                "error": f"claude.ai returned HTTP {e.code}",
                "detail": detail, "hint": hint,
            })
        except URLError as e:
            return write_json(self, 200, {"error": f"Network error: {e.reason}"})
        except Exception as e:
            return write_json(self, 200, {"error": str(e)})

        members = data.get("members", []) if isinstance(data, dict) else []
        return write_json(self, 200, {
            "members": members,
            "page": int(params["page"]),
            "page_size": int(params["page_size"]),
            "start_date": start_date,
            "count": len(members),
        })
