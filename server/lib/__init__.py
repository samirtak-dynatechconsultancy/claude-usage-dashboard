"""Shared modules for Vercel serverless functions.

Handlers in ``server/api/*.py`` import from this package via:

    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    from lib import auth, supabase_client, pricing
"""
