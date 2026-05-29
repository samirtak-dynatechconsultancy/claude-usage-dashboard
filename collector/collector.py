"""collector.py — Claude Code usage collector for end-user machines.

Scans Claude Code JSONL transcripts, uploads raw files to Supabase Storage,
and posts parsed metadata to the dashboard server. Designed to run silently
on a Windows Scheduled Task every 15 minutes.

Stdlib-only so PyInstaller produces a small, single-file .exe (no native
deps to ship). Talks to the server via HTTPS only — never Supabase directly.

Run manually for testing:

    python collector.py push                # one-shot push of new data
    python collector.py status              # show state + config, no upload
    python collector.py reset-state         # forget upload history (re-upload)

Reads config from (first match wins):
  1.  --config <path>
  2.  COLLECTOR_CONFIG env var
  3.  <exe-dir>\\config.json                (where the installer writes it)
  4.  %APPDATA%\\ClaudeUsageCollector\\config.json
  5.  ./config.json
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import socket
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


# ── Constants ───────────────────────────────────────────────────────────────

APP_NAME = "ClaudeUsageCollector"
USER_AGENT = "claude-usage-collector/1.0"
DEFAULT_PROJECTS_DIRS = [
    Path.home() / ".claude" / "projects",
    Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects",
]
INGEST_BATCH_SIZE = 100   # turns per POST /api/ingest call (keeps body < 4.5 MB)
HTTP_TIMEOUT_S = 60


# ── Config loading ──────────────────────────────────────────────────────────

def _exe_dir() -> Path:
    """Directory of the running script or PyInstaller bundle."""
    if getattr(sys, "frozen", False):           # PyInstaller
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _candidate_config_paths(override: Optional[str]) -> List[Path]:
    paths: List[Path] = []
    if override:
        paths.append(Path(override))
    env = os.environ.get("COLLECTOR_CONFIG")
    if env:
        paths.append(Path(env))
    paths.append(_exe_dir() / "config.json")
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / APP_NAME / "config.json")
    paths.append(Path.cwd() / "config.json")
    return paths


def load_config(override: Optional[str] = None) -> Tuple[Dict[str, Any], Path]:
    """Load config.json. Returns (config, path_used)."""
    for p in _candidate_config_paths(override):
        if p.is_file():
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            # Apply env-var overrides (useful for local testing).
            cfg.setdefault("server_url",   os.environ.get("SERVER_URL"))
            cfg.setdefault("ingest_token", os.environ.get("INGEST_TOKEN"))
            return cfg, p
    raise FileNotFoundError(
        "No config.json found. Searched:\n  " +
        "\n  ".join(str(x) for x in _candidate_config_paths(override))
    )


# ── Local state (what we've uploaded) ───────────────────────────────────────

def _state_path() -> Path:
    """Per-machine state file. Lives in %LOCALAPPDATA% so it persists across
    installs and doesn't need admin rights to write."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / APP_NAME / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_state() -> Dict[str, Any]:
    p = _state_path()
    if not p.is_file():
        return {"machine_fp": _make_machine_fp(), "files": {}}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"machine_fp": _make_machine_fp(), "files": {}}


def save_state(state: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def _make_machine_fp() -> str:
    """Stable per-machine identifier.

    uuid.getnode() returns the MAC address as a 48-bit int when one is found,
    otherwise a random value. We combine with the hostname so re-imaged
    machines (new MAC) still group differently from a coworker's box.
    """
    raw = f"{socket.gethostname()}|{uuid.getnode():012x}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Identification ──────────────────────────────────────────────────────────

def detect_user() -> Dict[str, str]:
    # getpass.getuser() consults LOGNAME/USER/LNAME/USERNAME env vars,
    # which is what we want — matches how the user knows themselves.
    import getpass
    return {"os_username": (getpass.getuser() or "unknown").strip()}


def detect_machine(machine_fp: str) -> Dict[str, str]:
    return {
        "hostname":   socket.gethostname(),
        "os":         f"{sys.platform} {os.environ.get('OS', '')}".strip(),
        "machine_fp": machine_fp,
    }


# ── JSONL parsing (mirror of scanner.py) ────────────────────────────────────

MODEL_PRIORITY = {"opus": 3, "sonnet": 2, "haiku": 1}


def _model_priority(model: Optional[str]) -> int:
    if not model:
        return 0
    m = model.lower()
    for kw, p in MODEL_PRIORITY.items():
        if kw in m:
            return p
    return 0


def _project_name_from_cwd(cwd: str) -> str:
    if not cwd:
        return "unknown"
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def parse_jsonl_file(filepath: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Same dedup-by-message_id logic as scanner.parse_jsonl_file."""
    seen_messages: Dict[str, Dict[str, Any]] = {}
    turns_no_id: List[Dict[str, Any]] = []
    session_meta: Dict[str, Dict[str, Any]] = {}
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user"):
                    continue
                session_id = record.get("sessionId")
                if not session_id:
                    continue

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_uuid":    session_id,
                        "project_name":    _project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp":  timestamp,
                        "git_branch":      git_branch,
                        "model":           None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"]  or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                if rtype == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    message_id = msg.get("id", "")

                    inp = usage.get("input_tokens", 0) or 0
                    out = usage.get("output_tokens", 0) or 0
                    cr  = usage.get("cache_read_input_tokens", 0) or 0
                    cc  = usage.get("cache_creation_input_tokens", 0) or 0
                    if inp + out + cr + cc == 0:
                        continue

                    tool_name = None
                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name")
                            break

                    if model:
                        prev = session_meta[session_id]["model"]
                        if _model_priority(model) > _model_priority(prev):
                            session_meta[session_id]["model"] = model
                        elif not prev:
                            session_meta[session_id]["model"] = model

                    turn = {
                        "session_uuid":          session_id,
                        "message_id":            message_id,
                        "timestamp":             timestamp,
                        "model":                 model,
                        "input_tokens":          inp,
                        "output_tokens":         out,
                        "cache_read_tokens":     cr,
                        "cache_creation_tokens": cc,
                        "tool_name":             tool_name,
                        "cwd":                   cwd,
                    }
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)
    except Exception as e:
        log(f"  warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, line_count


# ── HTTP ────────────────────────────────────────────────────────────────────

class CollectorError(Exception):
    pass


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None,
          timeout: int = HTTP_TIMEOUT_S) -> Tuple[int, bytes]:
    req = urlrequest.Request(url, data=body, method=method, headers={**headers, "User-Agent": USER_AGENT})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


def api_get_upload_url(cfg: Dict[str, Any], os_username: str, machine_fp: str,
                       content_hash: str) -> Dict[str, Any]:
    body = json.dumps({
        "os_username":  os_username,
        "machine_fp":   machine_fp,
        "content_hash": content_hash,
    }).encode("utf-8")
    status, raw = _http(
        "POST",
        cfg["server_url"].rstrip("/") + "/api/upload-url",
        headers={
            "Content-Type":   "application/json",
            "X-Ingest-Token": cfg["ingest_token"],
        },
        body=body,
    )
    if status != 200:
        raise CollectorError(f"/api/upload-url failed: HTTP {status} {raw[:200]!r}")
    return json.loads(raw)


def storage_put(upload_url: str, file_path: str, upload_token: Optional[str] = None) -> None:
    """PUT a file to a Supabase Storage signed upload URL.

    Supabase signed-upload URLs accept either:
      • PUT with `Authorization: Bearer <token>` (when create_signed_upload_url
        returned a token alongside the URL), OR
      • PUT to the URL alone if the token is already in the query string.
    We send both — extras are ignored.
    """
    with open(file_path, "rb") as f:
        body = f.read()
    headers = {"Content-Type": "application/octet-stream", "x-upsert": "true"}
    if upload_token:
        headers["Authorization"] = "Bearer " + upload_token
    status, raw = _http("PUT", upload_url, headers=headers, body=body, timeout=120)
    if status not in (200, 201):
        raise CollectorError(f"Storage upload failed: HTTP {status} {raw[:300]!r}")


def api_ingest(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    if len(body) > 4_400_000:
        raise CollectorError(f"Ingest payload too large ({len(body)} bytes); reduce batch size")
    status, raw = _http(
        "POST",
        cfg["server_url"].rstrip("/") + "/api/ingest",
        headers={
            "Content-Type":   "application/json",
            "X-Ingest-Token": cfg["ingest_token"],
        },
        body=body,
    )
    if status != 200:
        raise CollectorError(f"/api/ingest failed: HTTP {status} {raw[:300]!r}")
    return json.loads(raw)


# ── Hashing ─────────────────────────────────────────────────────────────────

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Logging ─────────────────────────────────────────────────────────────────

_LOG_FILE: Optional[Path] = None


def _log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / APP_NAME / "collector.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str) -> None:
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_FILE = _log_path()
    line = f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] {msg}"
    print(line, flush=True)
    try:
        # Rough log rotation: if the file exceeds 5 MB, truncate the head.
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > 5_000_000:
            with open(_LOG_FILE, "rb") as f:
                f.seek(-2_000_000, os.SEEK_END)
                tail = f.read()
            with open(_LOG_FILE, "wb") as f:
                f.write(tail)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Main push loop ──────────────────────────────────────────────────────────

def push(cfg: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Walk projects dirs, upload new content, post metadata, update state.

    Idempotent: if the (path, mtime) matches local state, skip. We do NOT
    rely on the server to dedupe; the server is the last line of defense.
    """
    state = load_state()
    machine_fp = state.get("machine_fp") or _make_machine_fp()
    state["machine_fp"] = machine_fp

    user = detect_user()
    machine = detect_machine(machine_fp)

    log(f"push: user={user['os_username']} machine={machine['hostname']} fp={machine_fp[:8]}…")

    # ── Discover files ────────────────────────────────────────────────────
    projects_dirs = [Path(p) for p in cfg.get("projects_dirs") or []] or DEFAULT_PROJECTS_DIRS
    jsonl_files: List[str] = []
    for d in projects_dirs:
        if not d.exists():
            continue
        jsonl_files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    jsonl_files.sort()
    log(f"discovered {len(jsonl_files)} jsonl files across {len(projects_dirs)} dirs")

    new_or_changed: List[str] = []
    for fp in jsonl_files:
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        rec = state["files"].get(fp)
        if rec and abs(rec.get("mtime", 0) - mtime) < 0.01:
            continue
        new_or_changed.append(fp)
    log(f"{len(new_or_changed)} files need upload")

    if dry_run:
        return {"would_upload": len(new_or_changed)}

    sessions_acc: Dict[str, Dict[str, Any]] = {}
    turns_acc: List[Dict[str, Any]] = []
    file_records: List[Dict[str, Any]] = []

    uploaded = 0
    skipped = 0

    for fp in new_or_changed:
        try:
            content_hash = sha256_file(fp)

            # If we already uploaded this hash, skip the upload but still parse
            # and submit turns (server dedupes on message_id).
            rec = state["files"].get(fp)
            need_upload = not rec or rec.get("content_hash") != content_hash

            if need_upload:
                signed = api_get_upload_url(cfg, user["os_username"], machine_fp, content_hash)
                storage_put(signed["upload_url"], fp, signed.get("upload_token"))
                content_path = signed["object_path"]
                uploaded += 1
            else:
                content_path = rec.get("content_path")
                skipped += 1

            metas, turns, lines = parse_jsonl_file(fp)
            for m in metas:
                # Keep first/last timestamps wide across multiple files.
                ex = sessions_acc.get(m["session_uuid"])
                if not ex:
                    sessions_acc[m["session_uuid"]] = m
                else:
                    if m["first_timestamp"] and (not ex["first_timestamp"] or m["first_timestamp"] < ex["first_timestamp"]):
                        ex["first_timestamp"] = m["first_timestamp"]
                    if m["last_timestamp"] and (not ex["last_timestamp"] or m["last_timestamp"] > ex["last_timestamp"]):
                        ex["last_timestamp"] = m["last_timestamp"]
                    if _model_priority(m["model"]) > _model_priority(ex["model"]):
                        ex["model"] = m["model"]
            for t in turns:
                t["content_path"] = content_path
            turns_acc.extend(turns)
            file_records.append({
                "path":         fp,
                "mtime":        os.path.getmtime(fp),
                "lines":        lines,
                "content_path": content_path,
            })

            state["files"][fp] = {
                "mtime":        os.path.getmtime(fp),
                "lines":        lines,
                "content_hash": content_hash,
                "content_path": content_path,
            }
        except Exception as e:
            log(f"  ERROR processing {fp}: {e}")
            log(traceback.format_exc())
            # Don't fail the whole push for one bad file — continue.
            continue

    log(f"uploads: {uploaded} new, {skipped} dedup-skipped (already in storage)")

    # ── Post metadata in batches ──────────────────────────────────────────
    if not turns_acc and not sessions_acc:
        log("nothing to ingest")
        save_state(state)
        return {"uploaded": uploaded, "skipped": skipped, "turns": 0, "sessions": 0}

    sessions_list = list(sessions_acc.values())
    batches_sent = 0

    # First batch carries all sessions + first chunk of turns; subsequent
    # batches carry empty sessions list. Cheaper than re-sending sessions
    # in every batch, and the server upserts safely either way.
    for i in range(0, max(1, len(turns_acc)), INGEST_BATCH_SIZE):
        chunk = turns_acc[i:i + INGEST_BATCH_SIZE]
        payload = {
            "user":            user,
            "machine":         machine,
            "sessions":        sessions_list if i == 0 else [],
            "turns":           chunk,
            "processed_files": file_records if i == 0 else [],
        }
        resp = api_ingest(cfg, payload)
        batches_sent += 1
        log(f"  batch {batches_sent}: {len(chunk)} turns ingested -> {resp.get('turns_received')}")

    save_state(state)
    log(f"push complete: {uploaded} files uploaded, {len(turns_acc)} turns across {len(sessions_list)} sessions, {batches_sent} batches")
    return {
        "uploaded":   uploaded,
        "skipped":    skipped,
        "turns":      len(turns_acc),
        "sessions":   len(sessions_list),
        "batches":    batches_sent,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_push(args):
    cfg, path = load_config(args.config)
    log(f"config loaded from {path}")
    push(cfg, dry_run=args.dry_run)


def cmd_status(args):
    try:
        cfg, path = load_config(args.config)
        print(f"Config:        {path}")
        print(f"Server URL:    {cfg.get('server_url')}")
        print(f"Ingest token:  {'set' if cfg.get('ingest_token') else 'MISSING'}")
        print(f"Projects dirs: {cfg.get('projects_dirs') or [str(p) for p in DEFAULT_PROJECTS_DIRS]}")
    except FileNotFoundError as e:
        print(str(e))

    state = load_state()
    print(f"\nMachine fp:    {state.get('machine_fp')}")
    print(f"State file:    {_state_path()}")
    print(f"Files tracked: {len(state.get('files', {}))}")
    print(f"Log file:      {_log_path()}")


def cmd_reset(args):
    p = _state_path()
    if p.exists():
        p.unlink()
        print(f"Removed {p}")
    else:
        print("No state to remove.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="collector", description="Claude Code usage collector")
    parser.add_argument("--config", help="Path to config.json (overrides search)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="Scan + upload new data to the server")
    p_push.add_argument("--dry-run", action="store_true", help="List what would be uploaded; don't push")
    p_push.set_defaults(func=cmd_push)

    p_status = sub.add_parser("status", help="Print config + state summary")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset-state", help="Forget all upload history (next push re-uploads everything)")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CollectorError as e:
        log(f"FATAL: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"UNHANDLED EXCEPTION: {e}")
        log(traceback.format_exc())
        sys.exit(2)


if __name__ == "__main__":
    main()
