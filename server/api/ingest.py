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


MODEL_PRIORITY = {"opus": 3, "sonnet": 2, "haiku": 1}

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


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not verify_ingest_token(self.headers.get("X-Ingest-Token")):
            return write_json(self, 401, {"error": "invalid ingest token"})

        body, err = read_json(self, max_bytes=5 * 1024 * 1024)  # bumped from 4 MB
        if err:
            return write_json(self, err[0], err[1])

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
        # See AGENTS.md — "now()" as a JSON literal fails Postgres parsing;
        # build the ISO timestamp here and reuse across the batch.
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── 1. Upsert user ──────────────────────────────────────────────────
        # is_rdp is sticky-true once seen (we never flip it back to false).
        user_payload = {"os_username": os_username, "last_seen": now_iso}
        if is_rdp:
            user_payload["is_rdp"] = True
        user_row = (
            sb.table("users")
            .upsert(user_payload, on_conflict="os_username")
            .execute()
        )
        user_id = user_row.data[0]["id"]

        # ── 2. Upsert machine ───────────────────────────────────────────────
        # Schema 0006: machines no longer has user_id (one box hosts many).
        machine_payload = {
            "hostname":   hostname,
            "os":         machine_in.get("os"),
            "machine_fp": machine_fp,
            "last_seen":  now_iso,
        }
        if is_rdp:
            machine_payload["is_rdp_host"] = True
        machine_row = (
            sb.table("machines")
            .upsert(machine_payload, on_conflict="machine_fp")
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

        # ── 8. Recompute session totals ─────────────────────────────────────
        for sid in affected_sessions:
            sb.rpc("recompute_session_totals", {"target_session_id": sid}).execute()

        return write_json(self, 200, {
            "ok": True,
            "user_id": user_id,
            "machine_id": machine_id,
            "sessions_seen":    len(sessions_in),
            "turns_received":   len(turns_in),
            "messages_received": len(records_in),
            "files_recorded":   len(files_in),
        })
