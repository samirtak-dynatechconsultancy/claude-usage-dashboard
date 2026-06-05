"""GET   /api/machines-admin         -- list machines with aliases + user mappings
PATCH  /api/machines-admin?id=X    -- update display_name / alias
POST   /api/machines-admin         -- create/update alias or user-machine mapping

Admin-only. Used by the dashboard's "Machines" tab.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.auth import verify_dashboard_user
from lib.http import write_json, read_json
from lib.supabase_client import service_client


def _require_admin(self):
    ok, email, role = verify_dashboard_user(self.headers.get("Authorization"))
    if not ok:
        write_json(self, 401, {"error": "not authorized"})
        return None
    if role != "admin":
        write_json(self, 403, {"error": "admin role required"})
        return None
    return email


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if _require_admin(self) is None:
            return

        sb = service_client()

        machines = (
            sb.table("machines")
            .select("id, hostname, user_id, machine_fp, os, is_rdp_host, "
                    "display_name, first_seen, last_seen")
            .order("hostname")
            .execute()
        ).data or []

        users = (
            sb.table("users")
            .select("id, os_username, display_name")
            .order("os_username")
            .execute()
        ).data or []
        users_by_id = {u["id"]: u for u in users}

        aliases = (
            sb.table("machine_aliases")
            .select("id, hostname, alias, notes, updated_at")
            .execute()
        ).data or []
        alias_by_hostname = {a["hostname"]: a for a in aliases}

        mappings = (
            sb.table("user_machine_map")
            .select("id, user_id, machine_id, role, notes")
            .execute()
        ).data or []

        turn_counts = {}
        tc_resp = sb.rpc("", {}).execute() if False else None
        # Count turns per machine for activity indication.
        # Use a raw count query grouped by machine_id.
        for m in machines:
            turn_counts[m["id"]] = 0
        tc_data = (
            sb.table("turns")
            .select("machine_id", count="exact")
            .execute()
        )

        rows = []
        for m in machines:
            u = users_by_id.get(m["user_id"], {})
            al = alias_by_hostname.get(m["hostname"], {})
            rows.append({
                "id":           m["id"],
                "hostname":     m["hostname"],
                "display_name": m.get("display_name") or al.get("alias") or "",
                "alias":        al.get("alias") or "",
                "alias_notes":  al.get("notes") or "",
                "machine_fp":   (m.get("machine_fp") or "")[:12],
                "os":           m.get("os") or "",
                "is_rdp_host":  bool(m.get("is_rdp_host")),
                "user_id":      m["user_id"],
                "user_label":   u.get("display_name") or u.get("os_username") or "?",
                "first_seen":   m.get("first_seen"),
                "last_seen":    m.get("last_seen"),
            })

        write_json(self, 200, {
            "machines": rows,
            "users": [{"id": u["id"],
                       "label": u.get("display_name") or u.get("os_username")}
                      for u in users],
            "mappings": mappings,
            "total": len(rows),
        })

    def do_PATCH(self):
        if _require_admin(self) is None:
            return

        qs = parse_qs(urlparse(self.path).query)
        machine_id = (qs.get("id") or [None])[0]
        if not machine_id:
            return write_json(self, 400, {"error": "id query param required"})

        body, err = read_json(self, max_bytes=4096)
        if err:
            return write_json(self, err[0], err[1])

        sb = service_client()

        # Update machine display_name directly.
        patch = {}
        if "display_name" in body:
            patch["display_name"] = (body["display_name"] or "").strip() or None

        if patch:
            sb.table("machines").update(patch).eq("id", machine_id).execute()

        # Upsert hostname alias.
        if "alias" in body:
            hostname = (body.get("hostname") or "").strip()
            alias = (body["alias"] or "").strip()
            if hostname:
                if alias:
                    sb.table("machine_aliases").upsert({
                        "hostname": hostname,
                        "alias": alias,
                        "notes": (body.get("alias_notes") or "").strip() or None,
                    }, on_conflict="hostname").execute()
                else:
                    sb.table("machine_aliases").delete().eq(
                        "hostname", hostname).execute()

        return write_json(self, 200, {"ok": True})

    def do_POST(self):
        """Create or delete a user-machine mapping."""
        if _require_admin(self) is None:
            return

        body, err = read_json(self, max_bytes=4096)
        if err:
            return write_json(self, err[0], err[1])

        action = (body.get("action") or "").strip()
        sb = service_client()

        if action == "add_mapping":
            user_id = body.get("user_id")
            machine_id = body.get("machine_id")
            role = (body.get("role") or "user").strip()
            notes = (body.get("notes") or "").strip() or None
            if not user_id or not machine_id:
                return write_json(self, 400, {"error": "user_id and machine_id required"})
            sb.table("user_machine_map").upsert({
                "user_id": user_id,
                "machine_id": machine_id,
                "role": role,
                "notes": notes,
            }, on_conflict="user_id,machine_id").execute()
            return write_json(self, 200, {"ok": True})

        if action == "remove_mapping":
            mapping_id = body.get("mapping_id")
            if not mapping_id:
                return write_json(self, 400, {"error": "mapping_id required"})
            sb.table("user_machine_map").delete().eq("id", mapping_id).execute()
            return write_json(self, 200, {"ok": True})

        return write_json(self, 400, {"error": "unknown action"})
