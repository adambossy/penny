"""Lazy DB singleton.

The agent-facing tools and the bootstrap step share one ``DB`` instance per
process. URL comes from ``$DATABASE_URL`` (defaults to a local SQLite file
at ``./penny.db`` for dev).
"""

from __future__ import annotations

import os

from .adapters.db.facade import DB

_DEFAULT_URL = "sqlite:///./penny.db"

_db: DB | None = None
_readonly_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        url = os.environ.get("DATABASE_URL", "").strip() or _DEFAULT_URL
        _db = DB(url, enforce_sqlite_fks=url.startswith("sqlite"))
    return _db


def get_readonly_db() -> DB:
    """DB handle for the agent's free-form ``run_sql``.

    Optionally bound to ``PENNY_AGENT_READONLY_DATABASE_URL`` (a Postgres role
    granted only ``SELECT``) as defense-in-depth on a user-owned Postgres, so a
    prompt-injected ``DELETE``/``UPDATE`` is rejected by the database itself,
    not merely by the parse guard. Unset (the default, and always on SQLite —
    no roles there) it falls back to the primary DB; the read-only SQL guard
    (``penny.security``) remains the write fence.
    """
    global _readonly_db
    if _readonly_db is None:
        url = os.environ.get("PENNY_AGENT_READONLY_DATABASE_URL", "").strip()
        if not url:
            return get_db()
        _readonly_db = DB(url, enforce_sqlite_fks=url.startswith("sqlite"))
    return _readonly_db
