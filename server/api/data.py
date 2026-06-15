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
from lib.pricing import calc_cost as _calc_cost
from lib.supabase_client import service_client


def _iso_day(ts) -> str:
    if not ts:
        return ""
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d")
    return str(ts)[:10]


def _iso_hour(ts) -> int:
    if not ts:
        return 0
    if hasattr(ts, "hour"):
        return ts.hour
    s = str(ts)
    if len(s) < 13:
        return 0
    try:
        return int(s[11:13])
    except ValueError:
        return 0


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            return self._handle()
        except Exception as exc:
            return write_json(self, 500, {"error": f"Server error: {exc}"})

    def _handle(self):
        ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})

        qs = parse_qs(urlparse(self.path).query)
        filter_user_id = (qs.get("user_id") or [None])[0]
        filter_hostname = (qs.get("hostname") or [None])[0]

        sb = service_client()

        # ── users (for the filter dropdown) ─────────────────────────────────
        # Fetch all users, then filter out those with zero turns below (after
        # we've loaded turns) so the dropdown only shows active users.
        all_users_raw = sb.table("users").select(
            "id, os_username, display_name, email, last_seen"
        ).order("os_username").execute().data or []

        # ── machines (for the filter dropdown) ──────────────────────────────
        all_machines_raw = sb.table("machines").select(
            "id, hostname, user_id, is_rdp_host, last_seen"
        ).order("hostname").execute().data or []

        # Build hostname → [machine_ids] map for hostname-based filtering.
        machine_ids_by_hostname = {}
        for m in all_machines_raw:
            machine_ids_by_hostname.setdefault(m["hostname"], []).append(m["id"])

        # Resolve hostname filter to the set of machine_ids it covers.
        filter_machine_ids = None
        if filter_hostname:
            filter_machine_ids = set(machine_ids_by_hostname.get(filter_hostname, []))

        # Deduplicated machines list for the dropdown (one per hostname).
        machines_deduped = []
        seen_hostnames = set()
        for m in all_machines_raw:
            if m["hostname"] in seen_hostnames:
                continue
            seen_hostnames.add(m["hostname"])
            machines_deduped.append(m)
        machines = all_machines_raw

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
        if filter_machine_ids:
            q = q.in_("machine_id", list(filter_machine_ids))
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
        # Derive session IDs from turns so we find sessions where the user
        # has turns even if the session row is owned by a different user_id
        # (common after migrations or on shared RDP hosts).
        turn_session_ids = list({
            t["session_id"] for t in all_turns if t.get("session_id")
        })

        sess_cols = (
            "id, session_uuid, user_id, machine_id, project_name, git_branch, "
            "first_timestamp, last_timestamp, model, turn_count, "
            "total_input_tokens, total_output_tokens, "
            "total_cache_read, total_cache_creation, title"
        )

        if filter_user_id:
            # Fetch sessions owned by this user AND sessions containing their
            # turns. On RDP hosts, session.user_id may differ from turn.user_id
            # so we need both queries to avoid missing rows.
            owned_ids = set()
            oq = sb.table("sessions").select("id").eq("user_id", filter_user_id)
            op = 0
            while True:
                chunk = oq.range(op * 1000, op * 1000 + 999).execute()
                if not chunk.data:
                    break
                owned_ids.update(r["id"] for r in chunk.data)
                if len(chunk.data) < 1000:
                    break
                op += 1

            combined_ids = list(owned_ids | set(turn_session_ids))
            sq = sb.table("sessions").select(sess_cols).order(
                "last_timestamp", desc=True)
            if combined_ids:
                sq = sq.in_("id", combined_ids)
            else:
                sq = sq.eq("user_id", filter_user_id)
        elif filter_machine_ids:
            sq = sb.table("sessions").select(sess_cols).order(
                "last_timestamp", desc=True
            ).in_("machine_id", list(filter_machine_ids))
        else:
            sq = sb.table("sessions").select(sess_cols).order(
                "last_timestamp", desc=True)

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
                                                        "turns": 0, "cost": 0})
                du["input"] += inp
                du["output"] += out
                du["cache_read"] += cr
                du["cache_creation"] += cc
                du["turns"] += 1
                du["cost"] += _calc_cost(model, inp, out, cr, cc)

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

        # Filter users to only those with at least one turn — keeps the
        # dropdown clean (no stale machine-name pseudo-users with zero data).
        user_ids_with_turns = {t["user_id"] for t in all_turns if t.get("user_id")}
        users = [u for u in all_users_raw if u["id"] in user_ids_with_turns]
        # Also include the currently filtered user even if they have no turns
        # in the current filter set (so the dropdown can show "clear" properly).
        if filter_user_id and filter_user_id not in user_ids_with_turns:
            for u in all_users_raw:
                if u["id"] == filter_user_id:
                    users.append(u)
                    break

        # ── sessions list for the table ─────────────────────────────────────
        # Build a quick lookup of user/machine display strings.
        users_by_id = {u["id"]: u for u in all_users_raw}
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
                "session_id":     str(s["session_uuid"] or "")[:8],
                "session_uuid":   str(s["session_uuid"] or ""),
                "user_id":        str(s["user_id"] or ""),
                "user_label":     user_label,
                "contributors":   contrib_labels,
                "machine_label":  mc.get("hostname") or "",
                "is_rdp":         bool(mc.get("is_rdp_host")),
                "project":        s.get("project_name") or "unknown",
                "branch":         s.get("git_branch") or "",
                # Raw ISO timestamps; the client formats in Asia/Kolkata for
                # display. "last" stays for backward compat with older clients
                # but new builds use last_timestamp directly.
                "last":             _iso_day(s.get("last_timestamp")),
                "last_timestamp":   str(s.get("last_timestamp") or ""),
                "first_timestamp":  str(s.get("first_timestamp") or ""),
                "last_date":      _iso_day(s.get("last_timestamp")),
                "model":          s.get("model") or "unknown",
                "turns":          s.get("turn_count") or 0,
                "input":          s.get("total_input_tokens") or 0,
                "output":         s.get("total_output_tokens") or 0,
                "cache_read":     s.get("total_cache_read") or 0,
                "cache_creation": s.get("total_cache_creation") or 0,
                "title":          s.get("title") or "",
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
                "hostname": m.get("hostname") or "",
                "is_rdp_host": bool(m.get("is_rdp_host")),
                "last_seen": m.get("last_seen"),
            } for m in machines_deduped],
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
