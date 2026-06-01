"""POST /api/summarize - generate (or fetch cached) AI summary for a session.

Body:  {"session_uuid": "..."}
Response: {"summary": "...", "cached": bool, "generated_at": "...", "model": "..."}

Calls Azure AI Foundry's Anthropic-native Messages API endpoint. Caches the
result onto the sessions row so subsequent clicks return instantly with no
extra Foundry spend.

Required Vercel env vars:
  AZURE_FOUNDRY_ENDPOINT  e.g. https://internalfoundry.services.ai.azure.com/anthropic/v1/messages
  AZURE_FOUNDRY_API_KEY   the API key from Foundry
  AZURE_FOUNDRY_MODEL     optional, defaults to claude-3-5-sonnet-20241022

Auth: same Supabase JWT pattern the dashboard already uses.
"""

import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib import request as urlrequest
from urllib.error import HTTPError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json, read_json
from lib.supabase_client import service_client


# Caps to keep request body and per-call cost in check.
MAX_RECORDS       = 60         # only the first N messages of the session
MAX_CHARS_PER_MSG = 4000       # truncate any single huge message
MAX_TOTAL_CHARS   = 80_000     # ~20K tokens of context, well within Sonnet's 200K
SUMMARY_MAX_TOK   = 400        # ~3 paragraphs at most
HTTP_TIMEOUT_S    = 45

DEFAULT_MODEL = "claude-3-5-sonnet-20241022"

SUMMARY_PROMPT = """\
You are summarizing a Claude Code session for a team-usage dashboard. The
viewer is a manager/admin who wants to understand what was attempted in
this session, not read the full conversation.

Write a 3-5 sentence summary covering:
  1. What task the user asked Claude Code to do
  2. The main approach or steps Claude took (mention key tools like Bash,
     Read, Edit, etc. by name when they shaped the work)
  3. Whether the task appears to have completed successfully, hit blockers,
     or was abandoned
  4. Any notable artifacts (files created/modified, commands run, errors hit)

Be concrete and specific. Reference the user's actual goal, not generic
phrases. Plain prose, no bullet points, no Markdown headers.

The session content follows below.

---
{content}
---

Summary:"""


def _extract_text_from_blocks(blocks):
    """Flatten message.content (list of blocks) into a single string."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", "") or "")
        elif t == "tool_use":
            name = b.get("name", "?")
            inp = b.get("input", {})
            parts.append(f"[tool_use: {name}] {json.dumps(inp)[:500]}")
        elif t == "tool_result":
            content = b.get("content")
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if isinstance(content, str):
                preview = content[:500]
                if len(content) > 500:
                    preview += "..."
                parts.append(f"[tool_result] {preview}")
    return "\n".join(parts)


def _build_session_text(messages):
    """Turn the messages rows into a transcript string for the model."""
    lines = []
    total = 0
    for m in messages[:MAX_RECORDS]:
        role = m.get("role") or "user"
        blocks = m.get("content_blocks") or m.get("text_content") or []
        text = _extract_text_from_blocks(blocks).strip()
        if not text:
            continue
        if len(text) > MAX_CHARS_PER_MSG:
            text = text[:MAX_CHARS_PER_MSG] + "...[truncated]"
        chunk = f"\n=== {role.upper()} ===\n{text}\n"
        if total + len(chunk) > MAX_TOTAL_CHARS:
            lines.append("\n[...further messages truncated due to size...]")
            break
        lines.append(chunk)
        total += len(chunk)
    return "".join(lines)


def _call_azure_foundry(content_text):
    endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT")
    api_key  = os.environ.get("AZURE_FOUNDRY_API_KEY")
    model    = os.environ.get("AZURE_FOUNDRY_MODEL", DEFAULT_MODEL)
    if not endpoint or not api_key:
        raise RuntimeError(
            "Missing AZURE_FOUNDRY_ENDPOINT or AZURE_FOUNDRY_API_KEY env var on the server"
        )

    body = json.dumps({
        "model":      model,
        "max_tokens": SUMMARY_MAX_TOK,
        "messages": [
            {"role": "user", "content": SUMMARY_PROMPT.format(content=content_text)}
        ],
    }).encode("utf-8")

    # Anthropic-native API on Azure: try x-api-key first (matches the public
    # Anthropic auth pattern, which is what /anthropic/v1/messages mirrors).
    # If Azure returns 401/403, retry with api-key (Azure standard) and then
    # with Authorization: Bearer as a last resort.
    last_err = None
    auth_styles = [
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        {"api-key":   api_key, "anthropic-version": "2023-06-01"},
        {"Authorization": f"Bearer {api_key}", "anthropic-version": "2023-06-01"},
    ]
    for headers_auth in auth_styles:
        headers = {"Content-Type": "application/json", **headers_auth}
        req = urlrequest.Request(endpoint, data=body, method="POST", headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
                # Anthropic Messages response shape:
                #   { "content": [{"type": "text", "text": "..."}], "stop_reason": ... }
                content = data.get("content") or []
                text_parts = [
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                summary = "\n".join(t for t in text_parts if t).strip()
                if not summary:
                    raise RuntimeError(f"Empty response from Foundry: {data}")
                return summary, model
        except HTTPError as e:
            # Only fall through to next auth style for auth errors.
            if e.code in (401, 403):
                last_err = f"HTTP {e.code} ({headers_auth}): {e.read()[:300]!r}"
                continue
            raise RuntimeError(f"Foundry HTTP {e.code}: {e.read()[:500]!r}")
        except Exception as e:
            raise RuntimeError(f"Foundry request failed: {e}")

    raise RuntimeError(f"All auth styles rejected by Foundry. Last error: {last_err}")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
        if not ok:
            return write_json(self, 401, {"error": "not authorized"})

        body, err = read_json(self, max_bytes=4096)
        if err:
            return write_json(self, err[0], err[1])

        session_uuid = (body.get("session_uuid") or "").strip()
        force_regen  = bool(body.get("force"))
        if not session_uuid:
            return write_json(self, 400, {"error": "session_uuid required"})

        sb = service_client()

        # ── Resolve session, check cache ────────────────────────────────────
        sess = (
            sb.table("sessions")
            .select("id, ai_summary, ai_summary_at, ai_summary_model")
            .eq("session_uuid", session_uuid).limit(1).execute()
        )
        if not sess.data:
            return write_json(self, 404, {"error": "session not found"})
        s = sess.data[0]

        if s.get("ai_summary") and not force_regen:
            return write_json(self, 200, {
                "summary":      s["ai_summary"],
                "cached":       True,
                "generated_at": s.get("ai_summary_at"),
                "model":        s.get("ai_summary_model"),
            })

        # ── Pull messages, build transcript ─────────────────────────────────
        msgs = (
            sb.table("messages")
            .select("role, content_blocks, text_content, timestamp")
            .eq("session_id", s["id"])
            .order("timestamp")
            .execute()
        )
        rows = msgs.data or []
        if not rows:
            return write_json(self, 400, {
                "error": "no message content for this session",
                "hint":  "Was this machine running in metadata-only mode (upload_content=false)?",
            })

        transcript = _build_session_text(rows)
        if not transcript.strip():
            return write_json(self, 400, {"error": "session messages contained no extractable text"})

        # ── Call Foundry ───────────────────────────────────────────────────
        try:
            summary, model_used = _call_azure_foundry(transcript)
        except Exception as e:
            return write_json(self, 502, {"error": str(e)})

        # ── Cache the result on sessions row ───────────────────────────────
        now_iso = datetime.now(timezone.utc).isoformat()
        sb.table("sessions").update({
            "ai_summary":        summary,
            "ai_summary_at":     now_iso,
            "ai_summary_model":  model_used,
        }).eq("id", s["id"]).execute()

        return write_json(self, 200, {
            "summary":      summary,
            "cached":       False,
            "generated_at": now_iso,
            "model":        model_used,
        })
