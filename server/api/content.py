"""GET /api/content - paginated conversation content + briefing for the modal.

Query params:
  session_uuid (required)
  offset  (default 0)
  limit   (default 10, max 100)

Response:
  {
    "records":  [ ...messages for this page... ],
    "total":    int,           // total messages in the session
    "offset":   int,
    "limit":    int,
    "briefing": { ... } | null  // only present when offset == 0
  }

When offset == 0 the response also carries:
  briefing.total_messages        (int)
  briefing.first_timestamp       (iso str | null)
  briefing.last_timestamp        (iso str | null)
  briefing.duration_min          (int)
  briefing.tools_used            [{ name, count }] sorted desc
  briefing.continuation_detected (bool) — first message starts with Claude
                                          Code's resume marker

Auth: Authorization: Bearer <Supabase JWT>. Email must be on dashboard_users.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json
from lib.supabase_client import service_client


# Cap how many messages a single request can ask for, so a malformed client
# can't make us ship 10k messages in one shot.
MAX_LIMIT = 100

# Claude Code injects this exact prefix when /compact, --resume, or auto-
# compaction summarizes prior context into a fresh first user message.
CONTINUATION_MARKER = "This session is being continued from a previous conversation"


def _compute_briefing(sb, session_id):
    """Aggregate stats across the entire session (run once on offset=0)."""
    # Total message count — `count="exact"` runs as a HEAD with Prefer:
    # count=exact, doesn't pull rows.
    count_resp = (
        sb.table("messages")
        .select("id", count="exact")
        .eq("session_id", session_id)
        .limit(0)
        .execute()
    )
    total = count_resp.count or 0

    # First and last timestamps — two cheap ORDER BY ... LIMIT 1 queries.
    first_resp = (
        sb.table("messages").select("timestamp")
        .eq("session_id", session_id).order("timestamp").limit(1).execute()
    )
    last_resp = (
        sb.table("messages").select("timestamp")
        .eq("session_id", session_id).order("timestamp", desc=True).limit(1).execute()
    )
    first_ts = first_resp.data[0]["timestamp"] if first_resp.data else None
    last_ts  = last_resp.data[0]["timestamp"]  if last_resp.data  else None

    duration_min = 0
    if first_ts and last_ts:
        # Hand-parse instead of importing datetime — the timestamps are
        # ISO 8601 from Postgres and arithmetic by string sort isn't safe
        # for the duration math, so fall back to a quick datetime parse.
        from datetime import datetime
        try:
            t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_min = max(0, round((t2 - t1).total_seconds() / 60))
        except Exception:
            duration_min = 0

    # Tools used — pull only the tool_uses column to keep payload small.
    # JSONB rows are typically <1 KB even for tool-heavy turns.
    tools_resp = (
        sb.table("messages").select("tool_uses")
        .eq("session_id", session_id).execute()
    )
    tool_counts = {}
    for m in tools_resp.data or []:
        for t in (m.get("tool_uses") or []):
            if isinstance(t, dict):
                name = t.get("name")
                if name:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
    tools_used = sorted(
        ({"name": n, "count": c} for n, c in tool_counts.items()),
        key=lambda x: -x["count"],
    )

    # Continuation marker — check the very first message's text_content.
    cont_resp = (
        sb.table("messages").select("text_content")
        .eq("session_id", session_id).order("timestamp").limit(1).execute()
    )
    cont = False
    if cont_resp.data:
        txt = (cont_resp.data[0].get("text_content") or "").lstrip()
        cont = txt.startswith(CONTINUATION_MARKER)

    return {
        "total_messages":        total,
        "first_timestamp":       first_ts,
        "last_timestamp":        last_ts,
        "duration_min":          duration_min,
        "tools_used":            tools_used,
        "continuation_detected": cont,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})

        qs = parse_qs(urlparse(self.path).query)
        session_uuid = (qs.get("session_uuid") or [None])[0]
        if not session_uuid:
            return write_json(self, 400, {"error": "session_uuid required"})

        try:
            offset = max(0, int((qs.get("offset") or ["0"])[0]))
            limit  = int((qs.get("limit")  or ["10"])[0])
        except ValueError:
            return write_json(self, 400, {"error": "offset and limit must be integers"})
        limit = max(1, min(MAX_LIMIT, limit))

        sb = service_client()

        # Look up session_id (uuid PK) from the public session_uuid.
        sess = (
            sb.table("sessions").select("id")
            .eq("session_uuid", session_uuid).limit(1).execute()
        )
        if not sess.data:
            return write_json(self, 404, {"error": "session not found"})
        sid = sess.data[0]["id"]

        # Briefing: only compute on the first page. Re-running it on every
        # scroll fetch would be expensive (extra COUNT + table scan).
        briefing = _compute_briefing(sb, sid) if offset == 0 else None
        total = briefing["total_messages"] if briefing else None
        if total is None:
            # offset > 0 — fetch count cheaply for the response shape.
            cnt = (
                sb.table("messages").select("id", count="exact")
                .eq("session_id", sid).limit(0).execute()
            )
            total = cnt.count or 0

        # Fetch this page of messages.
        msgs = (
            sb.table("messages")
            .select("role, timestamp, text_content, content_blocks, "
                    "tool_uses, tool_results, message_uuid")
            .eq("session_id", sid)
            .order("timestamp")
            .range(offset, offset + limit - 1)
            .execute()
        )

        records = []
        for m in (msgs.data or []):
            records.append({
                "type":      m["role"],
                "timestamp": m["timestamp"],
                "message": {
                    "id":      m.get("message_uuid"),
                    "content": m.get("content_blocks") or [],
                },
            })

        return write_json(self, 200, {
            "records":  records,
            "total":    total,
            "offset":   offset,
            "limit":    limit,
            "returned": len(records),
            "briefing": briefing,
        })
