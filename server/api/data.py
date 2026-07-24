"""GET /api/data — dashboard snapshot.

Returns the same shape as the local dashboard.py's /api/data, with two
additions:
  • per-user aggregation (this is multi-tenant now)
  • optional ?user_id= filter to drill into a specific person

Auth: Authorization: Bearer <Supabase JWT>. Email must be on dashboard_users.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

_CLAUDE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

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


def _iso_str(ts) -> str:
    """Full ISO timestamp for a datetime/str (or "" if empty). Used for the
    team-activity cookie-health timestamps sent to the client."""
    if not ts:
        return ""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


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

    def _team_activity(self, qs):
        """Serve per-user team activity from the DB.

        The collector (running daily on an admin's machine, where a valid
        claude.ai Cookie header lives) fetches /analytics/activity/users per org
        and pushes it to team_activity_daily via /api/ingest. The dashboard
        reads it here — no live claude.ai call (Cloudflare blocks Vercel's IP).

        Date filtering uses the dashboard's header RANGE selector: the frontend
        sends start/end (YYYY-MM-DD) and each user's per-day rows are aggregated
        across that range (sum counts/spend, sum days_active = active days in
        range, max last_active).

        Query params:
          org   — org UUID to show (default: org with the most recent success)
          start — inclusive range start YYYY-MM-DD (omit/blank = no lower bound)
          end   — inclusive range end   YYYY-MM-DD (omit/blank = no upper bound)
          sort  — chats|messages|days_active|spend  (default chats)
          order — asc|desc                          (default desc)
          page, page_size — client-side pager parity
        """
        def first(k, d):
            v = qs.get(k)
            return v[0] if v else d

        sb = service_client()

        # ── Orgs (dropdown + cookie health) ─────────────────────────────────
        orgs_raw = (sb.table("team_activity_org")
                    .select("org, org_name, ok, error, last_success_at, "
                            "last_attempt_at, member_count, source_host, os_user")
                    .order("org_name").execute().data) or []
        orgs = [{
            "org":             o.get("org"),
            "org_name":        o.get("org_name") or o.get("org"),
            "ok":              o.get("ok"),
            "error":           o.get("error"),
            "last_success_at": _iso_str(o.get("last_success_at")),
            "last_attempt_at": _iso_str(o.get("last_attempt_at")),
            "member_count":    o.get("member_count"),
            "source_host":     o.get("source_host"),
            "os_user":         o.get("os_user"),
        } for o in orgs_raw]

        if not orgs:
            return write_json(self, 200, {
                "orgs": [], "members": [],
                "error": "No team activity collected yet",
                "detail": "Configure analytics_orgs (org + full Cookie header) "
                          "in the collector's config.json and let the daily "
                          "ClaudeTeamActivityDaily task run once — or run "
                          "`ClaudeUsageCollector.exe team-activity` manually.",
            })

        # Selected org: requested, else the most recently successful one.
        requested_org = (first("org", "") or "").strip()
        valid_orgs = {o["org"] for o in orgs}
        if requested_org in valid_orgs:
            org = requested_org
        else:
            org = max(orgs, key=lambda o: o.get("last_success_at") or "")["org"]

        status = next((o for o in orgs if o["org"] == org), None)
        start = (first("start", "") or "").strip()
        end = (first("end", "") or "").strip()

        def _norm_key(email, name, uuid):
            e = (email or "").strip().lower()
            if e:
                return e
            if uuid:
                return f"uuid:{uuid}"
            return f"name:{(name or '').strip().lower()}" if name else None

        # ── All activity rows for the org (seat_tier pulled from the member
        # blob). One pass builds: the ever-active user set (with seat tier +
        # overall last-active), plus range-scoped activity aggregates. ────────
        SUM_INT = ("chat_count", "message_count", "projects_created_count",
                   "projects_used_count", "code_session_count")
        rows = (sb.table("team_activity_daily")
                .select("user_key, snapshot_date, name, email, role, "
                        "chat_count, message_count, projects_created_count, "
                        "projects_used_count, code_session_count, "
                        "estimated_spend_us_dollars, last_active, "
                        "member->>'seat_tier' AS seat_tier")
                .eq("org", org).execute().data) or []

        def _blank_member():
            m = {"name": None, "email": None, "role": None, "seat_tier": None,
                 "last_active": None, "estimated_spend_us_dollars": 0.0,
                 "days_active": 0}
            for f in SUM_INT:
                m[f] = 0
            return m

        agg = {}
        for r in rows:
            key = r.get("user_key") or _norm_key(r.get("email"), r.get("name"), None)
            if key is None:
                continue
            a = agg.get(key) or _blank_member()
            agg[key] = a
            a["name"] = a["name"] or r.get("name")
            a["email"] = a["email"] or r.get("email")
            a["role"] = a["role"] or r.get("role")
            a["seat_tier"] = a["seat_tier"] or r.get("seat_tier")
            # last_active = the user's most recent active day OVERALL (so a member
            # inactive in the selected range still shows when they were last seen).
            la = r.get("last_active")
            if la and (a["last_active"] is None or str(la) > str(a["last_active"])):
                a["last_active"] = la
            # Range-scoped activity: only rows whose day falls inside the range.
            d = _iso_day(r.get("snapshot_date"))
            if (not start or d >= start) and (not end or d <= end):
                for f in SUM_INT:
                    a[f] += int(r.get(f) or 0)
                a["days_active"] += 1   # one daily row = one active day in range
                try:
                    a["estimated_spend_us_dollars"] += float(
                        r.get("estimated_spend_us_dollars") or 0)
                except (TypeError, ValueError):
                    pass

        # ── Merge the full member roster (from /members) so seat holders who
        # have NEVER been active — no last_active, no activity rows — still show.
        roster = []
        try:
            rrow = (sb.table("team_activity_org").select("roster")
                    .eq("org", org).limit(1).execute().data)
            if rrow and isinstance(rrow[0].get("roster"), list):
                roster = rrow[0]["roster"]
        except Exception:
            roster = []
        for rm in roster:
            if not isinstance(rm, dict):
                continue
            key = _norm_key(rm.get("email"), rm.get("name"), rm.get("uuid"))
            if key is None:
                continue
            a = agg.get(key)
            if a is None:
                a = _blank_member()
                agg[key] = a
            # Roster is authoritative for identity + seat tier; keep activity's
            # last_active. (Roster has no last_active -> never-active stays blank.)
            a["name"] = a["name"] or rm.get("name")
            a["email"] = a["email"] or rm.get("email")
            a["role"] = a["role"] or rm.get("role")
            a["seat_tier"] = rm.get("seat_tier") or a["seat_tier"]

        members = list(agg.values())

        # ── Sort + paginate (parity with the old proxy contract) ────────────
        sort_key = {
            "chats": "chat_count", "messages": "message_count",
            "days_active": "days_active", "spend": "estimated_spend_us_dollars",
        }.get(first("sort", "chats"), "chat_count")
        reverse = first("order", "desc") != "asc"
        members.sort(key=lambda m: (m.get(sort_key) or 0), reverse=reverse)

        total = len(members)
        try:
            page = max(1, int(first("page", "1")))
            page_size = max(1, int(first("page_size", "50")))
        except ValueError:
            page, page_size = 1, 50
        pstart = (page - 1) * page_size
        page_members = members[pstart:pstart + page_size]

        return write_json(self, 200, {
            "orgs":       orgs,
            "org":        org,
            "org_name":   status.get("org_name") if status else org,
            "status":     status,
            "start":      start,
            "end":        end,
            "members":    page_members,
            "page":       page,
            "page_size":  page_size,
            "count":      len(page_members),
            "total":      total,
        })

    def _handle(self):
        ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})

        qs = parse_qs(urlparse(self.path).query)

        # Team Activity page: proxy claude.ai admin analytics. Folded in here
        # (instead of its own /api/team-activity.py) to stay under Vercel's
        # 12-function Hobby limit.
        if (qs.get("view") or [""])[0] == "team-activity":
            return self._team_activity(qs)

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

        # ── Claude Desktop usage data ──────────────────────────────────────
        usage_rows = []
        try:
            uq = sb.table("claude_usage_pr").select(
                "id, captured_at, email, org_id, session_pct, weekly_pct, "
                "five_hour_resets_at, seven_day_resets_at, host, os_user"
            ).order("captured_at", desc=True)
            page = 0
            while True:
                chunk = uq.range(page * 1000, page * 1000 + 999).execute()
                if not chunk.data:
                    break
                usage_rows.extend(chunk.data)
                if len(chunk.data) < 1000:
                    break
                page += 1
        except Exception:
            pass

        write_json(self, 200, {
            "viewer": {"email": email, "role": role},
            "usage": [{
                "captured_at":         r.get("captured_at"),
                "email":               r.get("email"),
                "session_pct":         r.get("session_pct"),
                "weekly_pct":          r.get("weekly_pct"),
                "five_hour_resets_at": r.get("five_hour_resets_at"),
                "seven_day_resets_at": r.get("seven_day_resets_at"),
                "host":                r.get("host"),
                "os_user":             r.get("os_user"),
            } for r in usage_rows],
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
