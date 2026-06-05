"""GET /api/data — dashboard snapshot.

Returns the same shape as the local dashboard.py's /api/data, with two
additions:
  • per-user aggregation (this is multi-tenant now)
  • optional ?user_id= filter to drill into a specific person

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


def _iso_day(ts: str) -> str:
    return (ts or "")[:10]


def _iso_hour(ts: str) -> int:
    if not ts or len(ts) < 13:
        return 0
    try:
        return int(ts[11:13])
    except ValueError:
        return 0


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})

        qs = parse_qs(urlparse(self.path).query)
        filter_user_id = (qs.get("user_id") or [None])[0]
        filter_machine_id = (qs.get("machine_id") or [None])[0]

        sb = service_client()

        # ── users (for the filter dropdown) ─────────────────────────────────
        users = sb.table("users").select(
            "id, os_username, display_name, email, last_seen"
        ).order("os_username").execute().data or []

        # ── machines (for the filter dropdown) ──────────────────────────────
        machines = sb.table("machines").select(
            "id, hostname, user_id, is_rdp_host, last_seen"
        ).order("hostname").execute().data or []

        # ── turns query base ────────────────────────────────────────────────
        # We pull all turns (filtered by user if given) and aggregate in
        # Python. For very large datasets this should move to an RPC, but at
        # team scale (< few million turns) it's fine.
        q = sb.table("turns").select(
            "id, session_id, user_id, machine_id, message_id, timestamp, model, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens"
        )
        if filter_user_id:
            q = q.eq("user_id", filter_user_id)
        if filter_machine_id:
            q = q.eq("machine_id", filter_machine_id)
        # Paginate — supabase-py defaults to 1000 rows. Loop until exhausted.
        all_turns = []
        page = 0
        while True:
            chunk = q.range(page * 1000, page * 1000 + 999).execute()
            if not chunk.data:
                break
            all_turns.extend(chunk.data)
            if len(chunk.data) < 1000:
                break
            page += 1

        # ── sessions ────────────────────────────────────────────────────────
        sq = sb.table("sessions").select(
            "id, session_uuid, user_id, machine_id, project_name, git_branch, "
            "first_timestamp, last_timestamp, model, turn_count, "
            "total_input_tokens, total_output_tokens, "
            "total_cache_read, total_cache_creation"
        ).order("last_timestamp", desc=True)
        if filter_user_id:
            sq = sq.eq("user_id", filter_user_id)
        if filter_machine_id:
            sq = sq.eq("machine_id", filter_machine_id)
        all_sessions = []
        page = 0
        while True:
            chunk = sq.range(page * 1000, page * 1000 + 999).execute()
            if not chunk.data:
                break
            all_sessions.extend(chunk.data)
            if len(chunk.data) < 1000:
                break
            page += 1

        # ── Aggregate: daily by model, hourly by model, daily by user ─────
        all_models_counts = {}
        daily_keyed = {}      # (day, model) → sums
        hourly_keyed = {}     # (day, hour, model) → sums
        daily_user_keyed = {} # (day, user_id) → sums

        for t in all_turns:
            model = t["model"] or "unknown"
            day = _iso_day(t["timestamp"])
            hour = _iso_hour(t["timestamp"])
            inp = t.get("input_tokens") or 0
            out = t.get("output_tokens") or 0
            cr = t.get("cache_read_tokens") or 0
            cc = t.get("cache_creation_tokens") or 0

            all_models_counts[model] = all_models_counts.get(model, 0) + inp + out

            dk = (day, model)
            d = daily_keyed.setdefault(dk, {"input": 0, "output": 0, "cache_read": 0,
                                            "cache_creation": 0, "turns": 0})
            d["input"] += inp
            d["output"] += out
            d["cache_read"] += cr
            d["cache_creation"] += cc
            d["turns"] += 1

            hk = (day, hour, model)
            h = hourly_keyed.setdefault(hk, {"output": 0, "turns": 0})
            h["output"] += out
            h["turns"] += 1

            # Per-user daily aggregation for the "Daily Usage by User" chart.
            uid = t.get("user_id")
            if uid:
                duk = (day, uid)
                du = daily_user_keyed.setdefault(duk, {"input": 0, "output": 0,
                                                        "cache_read": 0, "cache_creation": 0,
                                                        "turns": 0})
                du["input"] += inp
                du["output"] += out
                du["cache_read"] += cr
                du["cache_creation"] += cc
                du["turns"] += 1

        all_models = sorted(all_models_counts.keys(),
                            key=lambda m: -all_models_counts[m])
        daily_by_model = [
            {"day": d, "model": m, **vals}
            for (d, m), vals in sorted(daily_keyed.items())
        ]
        hourly_by_model = [
            {"day": d, "hour": h, "model": m, **vals}
            for (d, h, m), vals in sorted(hourly_keyed.items())
        ]

        # ── sessions list for the table ─────────────────────────────────────
        # Build a quick lookup of user/machine display strings.
        users_by_id = {u["id"]: u for u in users}
        machines_by_id = {m["id"]: m for m in machines}

        # Per-session contributor map: session_id → set of user_ids that
        # have turns in that session. Handles multi-user RDP sessions where
        # User A and User B both typed in the same Claude Code conversation.
        session_contributors = {}   # session_id → set(user_id)
        for t in all_turns:
            sid = t.get("session_id")
            uid = t.get("user_id")
            if sid and uid:
                session_contributors.setdefault(sid, set()).add(uid)

        sessions_all = []
        for s in all_sessions:
            u = users_by_id.get(s["user_id"], {})
            mc = machines_by_id.get(s["machine_id"], {})

            # Build a label showing all contributing users.
            contrib_ids = session_contributors.get(s["id"], set())
            contrib_labels = sorted(set(
                (users_by_id.get(uid, {}).get("display_name")
                 or users_by_id.get(uid, {}).get("os_username") or "?")
                for uid in contrib_ids
            )) if contrib_ids else []
            primary_label = u.get("display_name") or u.get("os_username") or "?"
            # user_label is always the single primary user (for chart grouping).
            # contributors carries all users for display in the session table.
            user_label = primary_label

            sessions_all.append({
                "session_id":     (s["session_uuid"] or "")[:8],
                "session_uuid":   s["session_uuid"],          # full UUID for drill-down
                "user_id":        s["user_id"],
                "user_label":     user_label,
                "contributors":   contrib_labels,
                "machine_label":  mc.get("hostname") or "",
                "project":        s.get("project_name") or "unknown",
                "branch":         s.get("git_branch") or "",
                # Raw ISO timestamps; the client formats in Asia/Kolkata for
                # display. "last" stays for backward compat with older clients
                # but new builds use last_timestamp directly.
                "last":             (s.get("last_timestamp") or "")[:16].replace("T", " "),
                "last_timestamp":   s.get("last_timestamp"),
                "first_timestamp":  s.get("first_timestamp"),
                "last_date":      (s.get("last_timestamp") or "")[:10],
                "model":          s.get("model") or "unknown",
                "turns":          s.get("turn_count") or 0,
                "input":          s.get("total_input_tokens") or 0,
                "output":         s.get("total_output_tokens") or 0,
                "cache_read":     s.get("total_cache_read") or 0,
                "cache_creation": s.get("total_cache_creation") or 0,
            })

        write_json(self, 200, {
            "viewer": {"email": email, "role": role},
            "users": [{
                "id": u["id"],
                "label": u.get("display_name") or u.get("os_username"),
                "os_username": u.get("os_username"),
                "email": u.get("email"),
                "last_seen": u.get("last_seen"),
            } for u in users],
            "machines": [{
                "id": m["id"],
                "hostname": m.get("hostname") or "",
                "is_rdp_host": bool(m.get("is_rdp_host")),
                "last_seen": m.get("last_seen"),
            } for m in machines],
            "all_models":      all_models,
            "daily_by_model":  daily_by_model,
            "daily_by_user":   [
                {"day": d, "user_id": uid,
                 "user_label": (users_by_id.get(uid, {}).get("display_name")
                                or users_by_id.get(uid, {}).get("os_username") or "?"),
                 **vals}
                for (d, uid), vals in sorted(daily_user_keyed.items())
            ],
            "hourly_by_model": hourly_by_model,
            "sessions":        sessions_all,
        })
