"""Database client — PostgreSQL via psycopg2.

Migrated from Supabase to direct PostgreSQL (Azure Database for PostgreSQL).
The DB class in lib/db.py provides a query-builder API that matches
supabase-py's .table().select().eq().execute() pattern, so API endpoints
need minimal changes.

Connection string comes from DATABASE_URL env var.
"""

from .db import get_db


def service_client():
    """Return the shared DB instance.

    Name kept as service_client() so existing API endpoint imports
    (`from lib.supabase_client import service_client`) work unchanged.
    """
    return get_db()
