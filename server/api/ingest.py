"""POST /api/ingest — receive a batch of metadata + parsed messages from a collector.

Postgres-only mode (no Supabase Storage). The collector sends every
new/changed JSONL line's message object inline; the server parses the
content blocks into structured columns (text_content, tool_uses,
tool_results) so the dashboard can query them with SQL.

Pipeline:
  1. Verify X-Ingest-Token
  2. Upsert user + machine
  3. Upsert sessions
  4. Bulk-insert turns (ON CONFLICT DO NOTHING on message_id)
  5. Build turn lookup map (message_id → turn_id) for the messages step
  6. Parse each `records` entry, bulk-insert into messages
  7. Record processed_files (collector-side state mirror)
  8. Recompute session totals (RPC)

Request body shape:
  {
    "user":     {"os_username": "samir.tak"},
    "machine":  {"hostname": "...", "os": "...", "machine_fp": "..."},
    "sessions": [
      {"session_uuid": "...", "project_name": "...", "git_branch": "...",
       "first_timestamp": "...", "last_timestamp": "...", "model": "..."}
    ],
    "turns": [
      {"session_uuid": "...", "message_id": "msg_...", "timestamp": "...",
       "model": "...", "input_tokens": 0, "output_tokens": 0,
       "cache_read_tokens": 0, "cache_creation_tokens": 0,
       "tool_name": null, "cwd": "..."}
    ],
    "records": [
      {"session_uuid": "...", "type": "user" | "assistant",
       "timestamp": "...", "message_uuid": null | "msg_...",
       "message": { ...raw message object from JSONL... }}
    ],
    "processed_files": [
      {"path": "...", "mtime": 1700000000.0, "lines": 42}
    ]
  }
"""

import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_ingest_token
from lib.http import write_json, read_json
from lib.supabase_client import service_client
from lib.title_generator import generate_title


MODEL_PRIORITY = {"opus": 3, "sonnet": 2, "haiku": 1}


def _strip_nulls(value):
    """Recursively strip U+0000 from every string in a parsed JSON structure.

    Postgres TEXT and JSONB columns both reject \\x00 with error 22P05
    ('unsupported Unicode escape sequence' / 'untranslatable character').
    Claude Code's JSONL sometimes contains null bytes -- typically when a
    Bash tool captures binary file content, or when a Read tool result
    includes a control char from a binary file. Stripping them lets the
    row land cleanly; the lossy substitution is preferable to dropping
    the entire push.
    """
    if isinstance(value, str):
        if "\x00" not in value:
            return value
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nulls(item) for item in value]
    return value

# tool_result content can be unbounded (shell output, file contents). Truncate
# at this size so a single rogue command output doesn't bloat the messages row.
TOOL_RESULT_MAX_CHARS = 8000


def _model_priority(model: str) -> int:
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def parse_message_content(content):
    """Parse a message.content (string OR list of blocks) into structured
    columns for the messages table.

    Returns dict with: text_content, content_blocks, tool_uses, tool_results.
    """
    text_parts = []
    blocks = []
    tool_uses = []
    tool_results = []

    if isinstance(content, str):
        text_parts.append(content)
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            blocks.append(b)
            t = b.get("type")
            if t == "text":
                text_parts.append(b.get("text", "") or "")
            elif t == "tool_use":
                tool_uses.append({
                    "id":    b.get("id"),
                    "name":  b.get("name"),
                    "input": b.get("input"),
                })
            elif t == "tool_result":
                # tool_result content can itself be string or nested array; flatten.
                result_content = b.get("content")
                if isinstance(result_content, list):
                    inner = []
                    for c in result_content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            inner.append(c.get("text", "") or "")
                    result_content = "\n".join(inner) if inner else json.dumps(result_content)
                if isinstance(result_content, str) and len(result_content) > TOOL_RESULT_MAX_CHARS:
                    result_content = result_content[:TOOL_RESULT_MAX_CHARS] + "\n…[truncated]"
                tool_results.append({
                    "tool_use_id": b.get("tool_use_id"),
                    "content":     result_content,
                    "is_error":    b.get("is_error", False),
                })

    text_content = "\n".join(p for p in text_parts if p).strip() or None
    return {
        "text_content":   text_content,
        "content_blocks": blocks,
        "tool_uses":      tool_uses or None,
        "tool_results":   tool_results or None,
    }


USAGE_TABLE = "claude_usage_pr"


def _as_int(v):
    """Coerce a metric to int; None/blank/garbage -> None (keep the column NULL
    rather than 0, so 'no data' is distinguishable from a real zero)."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _member_user_key(member, idx):
    """Stable per-user key within an org, preferred email -> name -> hash.

    Must be consistent across days so the same person's daily rows share a
    user_key (lets the dashboard build per-person trends)."""
    email = (member.get("email") or "").strip().lower()
    if email:
        return email
    name = (member.get("name") or "").strip().lower()
    if name:
        return f"name:{name}"
    for k in ("id", "user_id", "uuid", "account_uuid"):
        v = member.get(k)
        if v:
            return f"{k}:{v}"
    # Last resort: hash the member so at least it's deterministic for this push.
    import hashlib
    digest = hashlib.sha256(
        json.dumps(member, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"anon:{digest}"


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            return self._handle()
        except Exception as exc:
            return write_json(self, 500, {"error": f"Server error: {exc}"})

    def _handle_team_activity(self, body):
        """Store a DAILY claude.ai admin-analytics snapshot for one org, expanded
        into one row per user (pushed by the collector running on an admin's
        machine). See migration 0012.

        Body: {"kind":"team_activity", "org", "org_name", "snapshot_date",
               "ok", "error", "members":[...]}.

        On success (ok=true): upsert every member into team_activity_daily keyed
        by (org, snapshot_date, user_key) so a re-run the same day overwrites.
        Always update team_activity_org with the cookie/collection health so the
        dashboard can flag an expired cookie (ok=false) without wiping the last
        good day's data.
        """
        sb = service_client()
        now_iso = datetime.now(timezone.utc).isoformat()

        org = (body.get("org") or "").strip()
        if not org:
            return write_json(self, 400, {"error": "org is required"})
        org_name = body.get("org_name")
        # Normalize to a canonical YYYY-MM-DD (strip any time/zone suffix) so
        # that N admins pushing the SAME org on the SAME day always share one
        # snapshot_date -> the (org, snapshot_date, user_key) unique key dedups
        # them to a single row per person regardless of how each puller
        # formatted the date.
        snapshot_date = (body.get("snapshot_date") or now_iso[:10]).strip()[:10]
        ok = bool(body.get("ok", True))
        error = body.get("error")
        members = body.get("members") or []
        # Which machine pushed this (if the puller includes it). Lets you see
        # which admin device is sending each org's data. The collector sends
        # `source_host`; accept `host` too for forward/back compatibility.
        src_host = (body.get("source_host") or body.get("host") or "").strip() or None
        src_user = (body.get("os_user") or "").strip() or None

        inserted = 0
        if ok and members:
            rows = []
            seen_keys = set()
            for idx, m in enumerate(members):
                if not isinstance(m, dict):
                    continue
                key = _member_user_key(m, idx)
                # Dedupe within the push: never emit two rows for the same user
                # on the same day. (Earlier code suffixed the key with #idx to
                # dodge the unique constraint, but that created a spurious second
                # row -> inflated per-user day counts.) Keep the first occurrence.
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                rows.append({
                    "org":                        org,
                    "org_name":                   org_name,
                    "snapshot_date":              snapshot_date,
                    "captured_at":                now_iso,
                    "user_key":                   key,
                    "name":                       m.get("name"),
                    "email":                      m.get("email"),
                    "role":                       m.get("role"),
                    "chat_count":                 _as_int(m.get("chat_count")),
                    "message_count":              _as_int(m.get("message_count")),
                    "projects_created_count":     _as_int(m.get("projects_created_count")),
                    "projects_used_count":        _as_int(m.get("projects_used_count")),
                    "code_session_count":         _as_int(m.get("code_session_count")),
                    "days_active":                _as_int(m.get("days_active")),
                    "estimated_spend_us_dollars": m.get("estimated_spend_us_dollars"),
                    "last_active":                m.get("last_active"),
                    "source_host":                src_host,
                    "os_user":                    src_user,
                    "member":                     m,
                })
            if rows:
                sb.table("team_activity_daily").upsert(
                    rows, on_conflict="org,snapshot_date,user_key"
                ).execute()
                inserted = len(rows)

        status = {
            "org":             org,
            "org_name":        org_name,
            "last_attempt_at": now_iso,
            "ok":              ok,
            "error":           error,
        }
        # Only touch source_host/os_user when the push actually carries them, so
        # a push from an older collector (which doesn't send them) can't wipe a
        # device name a newer collector already recorded for this org.
        if src_host is not None:
            status["source_host"] = src_host
        if src_user is not None:
            status["os_user"] = src_user
        if ok:
            status["last_success_at"] = now_iso
            status["member_count"] = len(members)
        sb.table("team_activity_org").upsert(
            status, on_conflict="org"
        ).execute()

        return write_json(self, 200, {
            "ok": True, "org": org, "snapshot_date": snapshot_date,
            "rows_upserted": inserted, "cookie_ok": ok,
        })

    def _handle_team_roster(self, body):
        """Store the full member roster (from claude.ai /members) for one org on
        team_activity_org.roster, so the dashboard can show seat holders who have
        never been active. Only touches roster/org_name (+ device), never the
        cookie-health fields."""
        sb = service_client()
        now_iso = datetime.now(timezone.utc).isoformat()
        org = (body.get("org") or "").strip()
        if not org:
            return write_json(self, 400, {"error": "org is required"})
        roster = body.get("roster")
        if not isinstance(roster, list):
            roster = []
        payload = {
            "org":             org,
            "org_name":        body.get("org_name"),
            "roster":          roster,
            "last_attempt_at": now_iso,
        }
        src_host = (body.get("source_host") or body.get("host") or "").strip() or None
        src_user = (body.get("os_user") or "").strip() or None
        if src_host is not None:
            payload["source_host"] = src_host
        if src_user is not None:
            payload["os_user"] = src_user
        sb.table("team_activity_org").upsert(
            payload, on_conflict="org").execute()
        return write_json(self, 200, {"ok": True, "org": org,
                                      "roster_size": len(roster)})

    def _handle_team_activity_reset(self, body):
        """Delete stored team-activity rows so a backfill can repopulate cleanly.

        Body: {"kind":"team_activity_reset", "org": "<uuid>"?}. With `org`, only
        that org is cleared; without it, ALL team-activity data is wiped. Gated
        by the ingest token (same as every other ingest write)."""
        sb = service_client()
        org = (body.get("org") or "").strip()
        daily = sb.table("team_activity_daily").delete()
        status = sb.table("team_activity_org").delete()
        if org:
            daily = daily.eq("org", org)
            status = status.eq("org", org)
        daily_deleted = len(daily.execute().data or [])
        status.execute()
        return write_json(self, 200, {
            "ok": True, "reset": org or "all",
            "rows_deleted": daily_deleted,
        })

    def _handle_usage(self, body):
        sb = service_client()
        sb.table(USAGE_TABLE).insert({
            "email":                body.get("email"),
            "org_id":               body.get("org_id"),
            "session_pct":          body.get("session_pct"),
            "weekly_pct":           body.get("weekly_pct"),
            "five_hour_resets_at":  body.get("five_hour_resets_at"),
            "seven_day_resets_at":  body.get("seven_day_resets_at"),
            "host":                 body.get("host"),
            "os_user":              body.get("os_user"),
        }).execute()
        return write_json(self, 200, {"ok": True})

    def _handle(self):
        if not verify_ingest_token(self.headers.get("X-Ingest-Token")):
            return write_json(self, 401, {"error": "invalid ingest token"})

        body, err = read_json(self, max_bytes=5 * 1024 * 1024)  # bumped from 4 MB
        if err:
            return write_json(self, err[0], err[1])

        # ── Team-activity shortcut ─────────────────────────────────────────
        # The collector's `team-activity` subcommand sends {"kind":"team_activity"}
        # with a members array (or ok=false on cookie failure). Route it to the
        # team_activity_* tables and return early.
        if body.get("kind") == "team_activity":
            return self._handle_team_activity(body)

        # Reset: wipe stored team-activity data (all orgs, or one) so a
        # backfill can repopulate cleanly. Gated by the ingest token.
        if body.get("kind") == "team_activity_reset":
            return self._handle_team_activity_reset(body)

        # Roster: full member list (all seats, incl. never-active) for an org.
        if body.get("kind") == "team_roster":
            return self._handle_team_roster(body)

        # ── Usage-data shortcut ────────────────────────────────────────────
        # The collector's `usage` subcommand sends a flat object with
        # session_pct/weekly_pct. Route it to claude_usage_pr and return
        # early — no need for the full turns/sessions pipeline.
        if "session_pct" in body:
            return self._handle_usage(body)

        # Strip null bytes from every string in the parsed payload before any
        # DB call. Postgres rejects U+0000 in TEXT and JSONB columns; a
        # rogue null in a single tool_result content would otherwise tank
        # the entire batch with 22P05. See _strip_nulls() docstring.
        body = _strip_nulls(body)

        user_in     = body.get("user")     or {}
        machine_in  = body.get("machine")  or {}
        sessions_in = body.get("sessions") or []
        turns_in    = body.get("turns")    or []
        records_in  = body.get("records")  or []
        files_in    = body.get("processed_files") or []

        os_username = (user_in.get("os_username") or "").strip().lower()
        hostname   = (machine_in.get("hostname") or "").strip()
        machine_fp = (machine_in.get("machine_fp") or "").strip()
        if not os_username or not hostname or not machine_fp:
            return write_json(self, 400, {
                "error": "user.os_username, machine.hostname, machine.machine_fp are required"
            })

        # RDP / shared-OS-user fields (all optional, default to non-RDP)
        is_rdp           = bool(machine_in.get("is_rdp"))
        client_machine   = (machine_in.get("client_machine") or "").strip() or None
        rdp_session_id   = (machine_in.get("rdp_session_id") or machine_in.get("session_id") or "").strip() or None

        sb = service_client()

        # ── RDP auto-link ───────────────────────────────────────────────────
        # When an RDP push arrives carrying client_machine="DSPL-LPT-551",
        # look up whether that hostname is already registered in machines
        # (which would mean the teammate installed the collector on their
        # physical laptop too). If so, reuse that machine's user_id so the
        # RDP push gets attributed to the real human instead of creating a
        # "DSPL-LPT-551" pseudo-user.
        #
        # Falls back to the os_username from the payload (the CLIENTNAME
        # itself) when no laptop registration exists -- which is exactly
        # the current behavior, so non-laptop-installed teammates keep
        # working. Admins can still rename them via the Manage Users
        # modal as a manual override (Path A).
        rdp_autolinked = False
        if is_rdp and client_machine:
            try:
                hostname_match = (
                    sb.table("machines").select("user_id")
                    .eq("hostname", client_machine).limit(1).execute()
                )
                if hostname_match.data:
                    mapped_uid = hostname_match.data[0]["user_id"]
                    mapped_user = (
                        sb.table("users").select("os_username")
                        .eq("id", mapped_uid).limit(1).execute()
                    )
                    if mapped_user.data and mapped_user.data[0].get("os_username"):
                        os_username = mapped_user.data[0]["os_username"]
                        rdp_autolinked = True
            except Exception:
                # Lookup failure shouldn't fail the whole push; fall back to
                # the payload's os_username (which is the raw CLIENTNAME).
                pass

        # See AGENTS.md — "now()" as a JSON literal fails Postgres parsing;
        # build the ISO timestamp here and reuse across the batch.
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── 1. Upsert user ──────────────────────────────────────────────────
        # is_rdp is sticky-true once seen, BUT we only set it for genuinely
        # RDP-pseudo identities (CLIENTNAME-as-username). When auto-link
        # resolved a real human (their laptop registered them), don't
        # demote them to "RDP user" just because this particular push
        # came from an RDP host.
        user_payload = {"os_username": os_username, "last_seen": now_iso}
        if is_rdp and not rdp_autolinked:
            user_payload["is_rdp"] = True
        user_row = (
            sb.table("users")
            .upsert(user_payload, on_conflict="os_username")
            .execute()
        )
        user_id = user_row.data[0]["id"]

        # Auto-set display_name from machine_aliases if not already set.
        # Try hostname first, then os_username (for RDP users whose
        # username IS the client machine name, e.g. "dspl-lpt-551").
        if not user_row.data[0].get("display_name"):
            try:
                alias_row = (
                    sb.table("machine_aliases").select("alias")
                    .ilike("hostname", hostname).limit(1).execute()
                )
                if not alias_row.data:
                    alias_row = (
                        sb.table("machine_aliases").select("alias")
                        .ilike("hostname", os_username).limit(1).execute()
                    )
                if alias_row.data:
                    sb.table("users").update(
                        {"display_name": alias_row.data[0]["alias"]}
                    ).eq("id", user_id).execute()
            except Exception:
                pass

        # ── 2. Upsert machine ───────────────────────────────────────────────
        # Schema 0006 (revised): machines.user_id stays. The UNIQUE constraint
        # is now composite -- (machine_fp, user_id) -- so the same physical
        # RDP host can have N rows (one per user). on_conflict matches the
        # composite key.
        machine_payload = {
            "user_id":    user_id,
            "hostname":   hostname,
            "os":         machine_in.get("os"),
            "machine_fp": machine_fp,
            "last_seen":  now_iso,
        }
        if is_rdp:
            machine_payload["is_rdp_host"] = True
        machine_row = (
            sb.table("machines")
            .upsert(machine_payload, on_conflict="machine_fp,user_id")
            .execute()
        )
        machine_id = machine_row.data[0]["id"]

        # ── 3. Upsert sessions ──────────────────────────────────────────────
        session_id_map = {}
        if sessions_in:
            rows = []
            for s in sessions_in:
                if not s.get("session_uuid"):
                    continue
                rows.append({
                    "session_uuid":    s["session_uuid"],
                    "user_id":         user_id,
                    "machine_id":      machine_id,
                    "project_name":    s.get("project_name"),
                    "git_branch":      s.get("git_branch"),
                    "first_timestamp": s.get("first_timestamp"),
                    "last_timestamp":  s.get("last_timestamp"),
                    "model":           s.get("model"),
                    # Tag every session with the source-device info if the
                    # collector reported any. Lets the UI show "via RDP from
                    # LAPTOP-ALICE" per row.
                    "client_machine":  client_machine,
                    "rdp_session_id":  rdp_session_id,
                    "updated_at":      now_iso,
                })
            if rows:
                sess_resp = (
                    sb.table("sessions")
                    .upsert(rows, on_conflict="session_uuid")
                    .execute()
                )
                for r in sess_resp.data:
                    session_id_map[r["session_uuid"]] = r["id"]

        # Helper to look up a session_id given a session_uuid (uses local map
        # first, falls back to a DB query for sessions seen in earlier batches).
        def resolve_session_id(suuid):
            if not suuid:
                return None
            if suuid in session_id_map:
                return session_id_map[suuid]
            found = (
                sb.table("sessions").select("id")
                .eq("session_uuid", suuid).limit(1).execute()
            )
            if not found.data:
                return None
            session_id_map[suuid] = found.data[0]["id"]
            return session_id_map[suuid]

        # ── 4. Bulk insert turns ────────────────────────────────────────────
        affected_sessions = set()
        if turns_in:
            rows = []
            for t in turns_in:
                sid = resolve_session_id(t.get("session_uuid"))
                if not sid:
                    continue
                affected_sessions.add(sid)
                rows.append({
                    "session_id":            sid,
                    "user_id":               user_id,
                    "machine_id":            machine_id,
                    "message_id":            t.get("message_id") or None,
                    "timestamp":             t.get("timestamp"),
                    "model":                 t.get("model"),
                    "input_tokens":          int(t.get("input_tokens") or 0),
                    "output_tokens":         int(t.get("output_tokens") or 0),
                    "cache_read_tokens":     int(t.get("cache_read_tokens") or 0),
                    "cache_creation_tokens": int(t.get("cache_creation_tokens") or 0),
                    "tool_name":             t.get("tool_name"),
                    "cwd":                   t.get("cwd"),
                })
            if rows:
                # ON CONFLICT DO NOTHING against idx_turns_message_id.
                # (Schema migration 0003 made this index unconditional so the
                # PostgREST upsert path actually works.)
                (
                    sb.table("turns")
                    .upsert(rows, on_conflict="message_id", ignore_duplicates=True)
                    .execute()
                )

        # ── 5. Build turn lookup (message_id → turn_id) for messages step ───
        # Query the turns we either just inserted or that previous batches
        # inserted, since assistant messages link to their turn row.
        message_ids = [
            t.get("message_id") for t in turns_in if t.get("message_id")
        ]
        turn_id_by_msg = {}
        if message_ids:
            # Query in chunks to stay under PostgREST's URL length limit.
            CHUNK = 200
            for i in range(0, len(message_ids), CHUNK):
                ids_chunk = message_ids[i:i + CHUNK]
                tres = (
                    sb.table("turns").select("id, message_id")
                    .in_("message_id", ids_chunk).execute()
                )
                for r in (tres.data or []):
                    turn_id_by_msg[r["message_id"]] = r["id"]

        # ── 6. Parse & bulk-insert messages ─────────────────────────────────
        if records_in:
            msg_rows = []
            for r in records_in:
                sid = resolve_session_id(r.get("session_uuid"))
                if not sid:
                    continue
                msg_obj = r.get("message") or {}
                content = msg_obj.get("content")
                parsed = parse_message_content(content)
                msg_uuid = r.get("message_uuid") or msg_obj.get("id") or None
                tid = turn_id_by_msg.get(msg_uuid) if msg_uuid else None

                msg_rows.append({
                    "session_id":     sid,
                    "turn_id":        tid,
                    "user_id":        user_id,
                    "machine_id":     machine_id,
                    "message_uuid":   msg_uuid,
                    "role":           r.get("type") or msg_obj.get("role") or "user",
                    "timestamp":      r.get("timestamp"),
                    "text_content":   parsed["text_content"],
                    "content_blocks": parsed["content_blocks"],
                    "tool_uses":      parsed["tool_uses"],
                    "tool_results":   parsed["tool_results"],
                })
            if msg_rows:
                # Dedupe on message_uuid; NULLs are distinct so user messages
                # without an id never collide.
                (
                    sb.table("messages")
                    .upsert(msg_rows, on_conflict="message_uuid", ignore_duplicates=True)
                    .execute()
                )

        # ── 7. Record processed_files (collector state mirror) ──────────────
        if files_in:
            file_rows = []
            for f in files_in:
                if not f.get("path"):
                    continue
                file_rows.append({
                    "machine_id":  machine_id,
                    "path":        f["path"],
                    "mtime":       float(f.get("mtime") or 0.0),
                    "lines":       int(f.get("lines") or 0),
                    "uploaded_at": now_iso,
                    # content_path column survives in schema but is no longer used
                })
            if file_rows:
                (
                    sb.table("processed_files")
                    .upsert(file_rows, on_conflict="machine_id,path")
                    .execute()
                )

        # ── 8. Generate session titles (Haiku) ─────────────────────────────
        # For each session in this batch that has no title yet, grab the
        # first user message and call Haiku to produce a 3-7 word title.
        titles_generated = 0
        titles_failed = 0
        titles_no_text = 0
        if records_in:
            # Collect the first user message per session from this batch
            first_user_msg = {}
            for r in records_in:
                suuid = r.get("session_uuid")
                if not suuid or suuid in first_user_msg:
                    continue
                role = r.get("type") or (r.get("message") or {}).get("role") or ""
                if role != "user":
                    continue
                msg_obj = r.get("message") or {}
                content = msg_obj.get("content")
                if isinstance(content, str) and content.strip():
                    first_user_msg[suuid] = content.strip()
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    text = "\n".join(p for p in parts if p).strip()
                    if text:
                        first_user_msg[suuid] = text

            for suuid, text in first_user_msg.items():
                sid = session_id_map.get(suuid)
                if not sid:
                    continue
                try:
                    existing = (
                        sb.table("sessions").select("title")
                        .eq("id", sid).limit(1).execute()
                    )
                    if existing.data and existing.data[0].get("title"):
                        continue
                    title, _model = generate_title(text)
                    if title:
                        sb.table("sessions").update(
                            {"title": title}
                        ).eq("id", sid).execute()
                        titles_generated += 1
                    else:
                        titles_failed += 1
                except Exception as exc:
                    titles_failed += 1

            if not first_user_msg:
                titles_no_text = len(session_id_map)

        # ── 9. Recompute session totals ─────────────────────────────────────
        for sid in affected_sessions:
            sb.rpc("recompute_session_totals", {"target_session_id": sid}).execute()

        return write_json(self, 200, {
            "ok": True,
            "user_id": user_id,
            "machine_id": machine_id,
            "rdp_autolinked": rdp_autolinked,
            "resolved_user": os_username,
            "sessions_seen":    len(sessions_in),
            "turns_received":   len(turns_in),
            "messages_received": len(records_in),
            "files_recorded":   len(files_in),
            "titles_generated": titles_generated,
            "titles_failed":   titles_failed,
            "titles_no_text":  titles_no_text,
        })
