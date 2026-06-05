"""PostgreSQL adapter with a query-builder API matching supabase-py.

Provides table().select().eq().execute() etc. so API endpoints need minimal
rewrites when switching from Supabase to direct PostgreSQL (Azure, RDS, etc.).

Connection string comes from DATABASE_URL env var. SSL is enabled by default
(Azure PostgreSQL requires it).

Usage:
    from lib.db import get_db
    db = get_db()
    rows = db.table("users").select("id, name").eq("active", True).execute()
    # rows.data = [{...}, ...]
    # rows.count = N  (when count="exact" is passed to select)
"""

import os
import json
import threading

import psycopg2
import psycopg2.extras

# ── Connection pool (module-scope for Vercel warm-container reuse) ────────
_conn = None
_lock = threading.Lock()


def _get_conn():
    """Return a cached connection, reconnecting if stale."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                # Cheap liveness check
                _conn.cursor().execute("SELECT 1")
            except Exception:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None

        if _conn is None:
            url = os.environ.get("DATABASE_URL", "")
            if not url:
                raise RuntimeError(
                    "Missing DATABASE_URL environment variable. "
                    "Set it in Vercel → Project Settings → Environment Variables."
                )
            # Normalize SQLAlchemy-style URLs:
            #   postgresql+asyncpg://...  →  postgresql://...
            #   postgresql+psycopg2://... →  postgresql://...
            if url.startswith("postgresql+"):
                url = "postgresql://" + url.split("://", 1)[1]
            # Ensure sslmode is set for Azure
            if "sslmode" not in url and "ssl" not in url:
                sep = "&" if "?" in url else "?"
                url += sep + "sslmode=require"
            elif "?ssl=require" in url:
                url = url.replace("?ssl=require", "?sslmode=require")
            elif "&ssl=require" in url:
                url = url.replace("&ssl=require", "&sslmode=require")

            _conn = psycopg2.connect(url)
            _conn.autocommit = True
        return _conn


# ── Result object ────────────────────────────────────────────────────────
class QueryResult:
    """Mimics supabase-py's APIResponse with .data and .count."""
    __slots__ = ("data", "count")

    def __init__(self, data=None, count=None):
        self.data = data or []
        self.count = count


# ── Query Builder ────────────────────────────────────────────────────────
class _NotFilter:
    """Handles .not_.is_("col", "null") pattern from supabase-py."""
    def __init__(self, qb):
        self._qb = qb

    def is_(self, col, val):
        if val == "null":
            self._qb._wheres.append(f'"{col}" IS NOT NULL')
        else:
            self._qb._wheres.append(f'"{col}" IS NOT %s')
            self._qb._params.append(val)
        return self._qb


class QueryBuilder:
    def __init__(self, conn, table_name):
        self._conn = conn
        self._table = table_name
        self._mode = "select"  # select | insert | update | delete | upsert
        self._select_cols = "*"
        self._count_mode = None
        self._wheres = []
        self._params = []
        self._order_col = None
        self._order_desc = False
        self._limit_val = None
        self._offset_val = None
        self._range_start = None
        self._range_end = None
        self._upsert_data = None
        self._upsert_conflict = None
        self._upsert_ignore = False
        self._update_data = None
        self._insert_data = None

    @property
    def not_(self):
        return _NotFilter(self)

    def select(self, cols="*", count=None):
        self._mode = "select"
        self._select_cols = cols
        self._count_mode = count
        return self

    def eq(self, col, val):
        self._wheres.append(f'"{col}" = %s')
        self._params.append(val)
        return self

    def neq(self, col, val):
        self._wheres.append(f'"{col}" != %s')
        self._params.append(val)
        return self

    def in_(self, col, vals):
        if not vals:
            # Empty IN → no matches
            self._wheres.append("FALSE")
        else:
            placeholders = ", ".join(["%s"] * len(vals))
            self._wheres.append(f'"{col}" IN ({placeholders})')
            self._params.extend(vals)
        return self

    def order(self, col, desc=False):
        self._order_col = col
        self._order_desc = desc
        return self

    def limit(self, n):
        self._limit_val = n
        return self

    def range(self, start, end):
        self._range_start = start
        self._range_end = end
        return self

    def upsert(self, data, on_conflict=None, ignore_duplicates=False):
        self._mode = "upsert"
        self._upsert_data = data if isinstance(data, list) else [data]
        self._upsert_conflict = on_conflict
        self._upsert_ignore = ignore_duplicates
        return self

    def update(self, data):
        self._mode = "update"
        self._update_data = data
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def execute(self):
        if self._mode == "select":
            return self._exec_select()
        elif self._mode == "upsert":
            return self._exec_upsert()
        elif self._mode == "update":
            return self._exec_update()
        elif self._mode == "delete":
            return self._exec_delete()
        raise ValueError(f"Unknown mode: {self._mode}")

    # ── SELECT ──────────────────────────────────────────────────────────
    def _exec_select(self):
        cols = self._select_cols.strip()
        where_sql, params = self._build_where()

        # Count-only query (supabase count="exact" with limit(0))
        if self._count_mode == "exact" and self._limit_val == 0:
            sql = f'SELECT COUNT(*) FROM "{self._table}"'
            if where_sql:
                sql += " WHERE " + where_sql
            cur = self._conn.cursor()
            cur.execute(sql, params)
            cnt = cur.fetchone()[0]
            cur.close()
            return QueryResult(data=[], count=cnt)

        sql = f'SELECT {cols} FROM "{self._table}"'
        if where_sql:
            sql += " WHERE " + where_sql
        if self._order_col:
            direction = "DESC" if self._order_desc else "ASC"
            sql += f' ORDER BY "{self._order_col}" {direction}'
        if self._range_start is not None and self._range_end is not None:
            sql += f" LIMIT {self._range_end - self._range_start + 1} OFFSET {self._range_start}"
        elif self._limit_val is not None:
            sql += f" LIMIT {self._limit_val}"

        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()

        # Convert to plain dicts (RealDictRow is dict-like but not exactly dict)
        data = [dict(r) for r in rows]

        result = QueryResult(data=data)
        if self._count_mode == "exact":
            # Also run a count query
            count_sql = f'SELECT COUNT(*) FROM "{self._table}"'
            if where_sql:
                count_sql += " WHERE " + where_sql
            cur2 = self._conn.cursor()
            cur2.execute(count_sql, params)
            result.count = cur2.fetchone()[0]
            cur2.close()
        return result

    # ── UPSERT ──────────────────────────────────────────────────────────
    def _exec_upsert(self):
        if not self._upsert_data:
            return QueryResult()

        # All rows must have the same keys
        keys = list(self._upsert_data[0].keys())
        cols_sql = ", ".join(f'"{k}"' for k in keys)
        placeholders = ", ".join(["%s"] * len(keys))

        conflict_cols = self._upsert_conflict or ""
        conflict_sql = ", ".join(f'"{c.strip()}"' for c in conflict_cols.split(",")) if conflict_cols else ""

        if self._upsert_ignore and conflict_sql:
            sql = (
                f'INSERT INTO "{self._table}" ({cols_sql}) VALUES ({placeholders}) '
                f"ON CONFLICT ({conflict_sql}) DO NOTHING "
                f"RETURNING *"
            )
        elif conflict_sql:
            update_cols = ", ".join(
                f'"{k}" = EXCLUDED."{k}"' for k in keys
                if k not in [c.strip() for c in conflict_cols.split(",")]
            )
            if update_cols:
                sql = (
                    f'INSERT INTO "{self._table}" ({cols_sql}) VALUES ({placeholders}) '
                    f"ON CONFLICT ({conflict_sql}) DO UPDATE SET {update_cols} "
                    f"RETURNING *"
                )
            else:
                sql = (
                    f'INSERT INTO "{self._table}" ({cols_sql}) VALUES ({placeholders}) '
                    f"ON CONFLICT ({conflict_sql}) DO NOTHING "
                    f"RETURNING *"
                )
        else:
            sql = (
                f'INSERT INTO "{self._table}" ({cols_sql}) VALUES ({placeholders}) '
                f"RETURNING *"
            )

        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        all_rows = []
        for row in self._upsert_data:
            vals = [self._serialize(row[k]) for k in keys]
            cur.execute(sql, vals)
            returned = cur.fetchall()
            all_rows.extend(dict(r) for r in returned)
        cur.close()

        return QueryResult(data=all_rows)

    # ── UPDATE ──────────────────────────────────────────────────────────
    def _exec_update(self):
        if not self._update_data:
            return QueryResult()

        set_parts = []
        params = []
        for k, v in self._update_data.items():
            set_parts.append(f'"{k}" = %s')
            params.append(self._serialize(v))

        where_sql, where_params = self._build_where()
        params.extend(where_params)

        sql = f'UPDATE "{self._table}" SET {", ".join(set_parts)}'
        if where_sql:
            sql += " WHERE " + where_sql
        sql += " RETURNING *"

        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return QueryResult(data=[dict(r) for r in rows])

    # ── DELETE ──────────────────────────────────────────────────────────
    def _exec_delete(self):
        where_sql, params = self._build_where()
        sql = f'DELETE FROM "{self._table}"'
        if where_sql:
            sql += " WHERE " + where_sql
        sql += " RETURNING *"

        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return QueryResult(data=[dict(r) for r in rows])

    # ── Helpers ─────────────────────────────────────────────────────────
    def _build_where(self):
        if not self._wheres:
            return "", []
        # Separate raw SQL where clauses from parameterized ones
        return " AND ".join(self._wheres), list(self._params)

    @staticmethod
    def _serialize(val):
        """Convert Python dicts/lists to JSON strings for JSONB columns."""
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        return val


# ── RPC caller ───────────────────────────────────────────────────────────
class _RPCCall:
    """Mimics sb.rpc("func", params).execute()."""
    def __init__(self, conn, func_name, params):
        self._conn = conn
        self._func = func_name
        self._params = params

    def execute(self):
        # Build: SELECT func_name(param1 := val1, param2 := val2)
        if self._params:
            args = ", ".join(
                f"{k} := %s" for k in self._params.keys()
            )
            sql = f"SELECT {self._func}({args})"
            vals = list(self._params.values())
        else:
            sql = f"SELECT {self._func}()"
            vals = []

        cur = self._conn.cursor()
        cur.execute(sql, vals)
        result = cur.fetchone()
        cur.close()
        return QueryResult(data=[{"result": result[0] if result else None}])


# ── Main DB class ────────────────────────────────────────────────────────
class DB:
    """Drop-in replacement for supabase.Client's data operations."""

    def __init__(self, conn):
        self._conn = conn

    def table(self, name):
        return QueryBuilder(self._conn, name)

    def rpc(self, func_name, params=None):
        return _RPCCall(self._conn, func_name, params or {})


# ── Public interface ─────────────────────────────────────────────────────
_db_instance = None


def get_db() -> DB:
    """Return a cached DB instance (warm-container reuse on Vercel)."""
    global _db_instance
    if _db_instance is None:
        _db_instance = DB(_get_conn())
    return _db_instance
