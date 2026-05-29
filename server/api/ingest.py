"""POST /api/ingest — receive a batch of metadata from a collector.

The collector uploads raw JSONL files directly to Supabase Storage first
(via /api/upload-url), then posts the parsed metadata here. We:

  1. Verify X-Ingest-Token (shared team secret)
  2. Upsert user (by os_username) and machine (by machine_fp)
  3. Upsert sessions, then bulk-insert turns with ON CONFLICT DO NOTHING
  4. Recompute session totals from the actual turns table — this matches the
     reconciliation pass in scanner.py, needed because dedupe via ON CONFLICT
     would otherwise let session totals drift on retries.

Request body:
  {
    "user":     {"os_username": "samir.tak"},
    "machine":  {"hostname": "SAMIR-DESKTOP", "os": "Windows 11",
                 "machine_fp": "<stable hash>"},
    "sessions": [
      {"session_uuid": "...", "project_name": "...", "git_branch": "...",
       "first_timestamp": "...", "last_timestamp": "...", "model": "..."}
    ],
    "turns": [
      {"session_uuid": "...", "message_id": "...", "timestamp": "...",
       "model": "...", "input_tokens": 0, "output_tokens": 0,
       "cache_read_tokens": 0, "cache_creation_tokens": 0,
       "tool_name": null, "cwd": "...", "content_path": "raw/.../file.jsonl"}
    ],
    "processed_files": [
      {"path": "...", "mtime": 1700000000.0, "lines": 42,
       "content_path": "raw/.../file.jsonl"}
    ]
  }
"""

import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_ingest_token
from lib.http import write_json, read_json
from lib.supabase_client import service_client


MODEL_PRIORITY = {"opus": 3, "sonnet": 2, "haiku": 1}


def _model_priority(model: str) -> int:
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def _pick_primary_model(existing: str, incoming: str) -> str:
    """opus > sonnet > haiku, with ties resolved to the incoming value."""
    if _model_priority(incoming) > _model_priority(existing):
        return incoming
    return existing or incoming


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not verify_ingest_token(self.headers.get("X-Ingest-Token")):
            return write_json(self, 401, {"error": "invalid ingest token"})

        body, err = read_json(self)
        if err:
            return write_json(self, err[0], err[1])

        user_in = body.get("user") or {}
        machine_in = body.get("machine") or {}
        sessions_in = body.get("sessions") or []
        turns_in = body.get("turns") or []
        files_in = body.get("processed_files") or []

        os_username = (user_in.get("os_username") or "").strip().lower()
        hostname = (machine_in.get("hostname") or "").strip()
        machine_fp = (machine_in.get("machine_fp") or "").strip()
        if not os_username or not hostname or not machine_fp:
            return write_json(self, 400, {"error": "user.os_username, machine.hostname, machine.machine_fp are required"})

        sb = service_client()

        # PostgREST receives JSON values as literals, so we cannot send the
        # string "now()" and expect Postgres to evaluate it — only 'now' is
        # a magic timestamp literal, 'now()' fails parsing. Build an ISO
        # timestamp here in Python and reuse it across this batch.
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── 1. Upsert user ──────────────────────────────────────────────────
        user_row = (
            sb.table("users")
            .upsert(
                {"os_username": os_username, "last_seen": now_iso},
                on_conflict="os_username",
            )
            .execute()
        )
        # supabase-py returns the upserted row in .data
        user_id = user_row.data[0]["id"]

        # ── 2. Upsert machine ──────────────────────────────────────────────
        machine_row = (
            sb.table("machines")
            .upsert(
                {
                    "user_id": user_id,
                    "hostname": hostname,
                    "os": machine_in.get("os"),
                    "machine_fp": machine_fp,
                    "last_seen": now_iso,
                },
                on_conflict="machine_fp",
            )
            .execute()
        )
        machine_id = machine_row.data[0]["id"]

        # ── 3. Upsert sessions ──────────────────────────────────────────────
        # Build a session_uuid → session_id map for the turns step.
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

                # Fix primary model when an existing session already had a
                # higher-priority one (opus). upsert overwrites with whatever
                # we sent, so we re-pick after the fact.
                for r in sess_resp.data:
                    incoming = r.get("model")
                    # Already in r since we just upserted — but to apply the
                    # priority rule, we'd need the *prior* value. Skip and rely
                    # on the recompute step at the end (it picks the most
                    # common model among the session's turns).

        # ── 4. Bulk insert turns ───────────────────────────────────────────
        affected_sessions = set()
        if turns_in:
            rows = []
            for t in turns_in:
                suuid = t.get("session_uuid")
                sid = session_id_map.get(suuid)
                if not sid:
                    # Session might already exist from a prior ingest; look it up.
                    found = (
                        sb.table("sessions")
                        .select("id")
                        .eq("session_uuid", suuid)
                        .limit(1)
                        .execute()
                    )
                    if not found.data:
                        continue
                    sid = found.data[0]["id"]
                    session_id_map[suuid] = sid

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
                    "content_path":          t.get("content_path"),
                    "content_offset":        t.get("content_offset"),
                })
            if rows:
                # ignore_duplicates uses ON CONFLICT DO NOTHING against the
                # message_id unique partial index.
                (
                    sb.table("turns")
                    .upsert(rows, on_conflict="message_id", ignore_duplicates=True)
                    .execute()
                )

        # ── 5. Record processed_files (collector-side state mirror) ────────
        if files_in:
            file_rows = []
            for f in files_in:
                if not f.get("path"):
                    continue
                file_rows.append({
                    "machine_id":   machine_id,
                    "path":         f["path"],
                    "mtime":        float(f.get("mtime") or 0.0),
                    "lines":        int(f.get("lines") or 0),
                    "content_path": f.get("content_path"),
                    "uploaded_at":  now_iso,
                })
            if file_rows:
                (
                    sb.table("processed_files")
                    .upsert(file_rows, on_conflict="machine_id,path")
                    .execute()
                )

        # ── 6. Recompute session totals from actual turns ──────────────────
        # The local scanner does the same — INSERT...ON CONFLICT skips dupes,
        # but session totals would drift if we added additively. Use a stored
        # procedure (defined below in 0002) OR do per-session recompute here.
        #
        # For now, do it per-session in a single RPC call.
        for sid in affected_sessions:
            sb.rpc("recompute_session_totals", {"target_session_id": sid}).execute()

        return write_json(self, 200, {
            "ok": True,
            "user_id": user_id,
            "machine_id": machine_id,
            "sessions_seen": len(sessions_in),
            "turns_received": len(turns_in),
            "files_recorded": len(files_in),
        })
