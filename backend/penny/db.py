"""Lazy DB singleton.

The agent-facing tools and the bootstrap step share one ``DB`` instance per
process. URL comes from ``$PENNY_DATABASE_URL`` (or ``$DATABASE_URL``;
defaults to a local SQLite file at ``./penny.db``), normalized so users can
paste whatever their provider handed them.
"""

from __future__ import annotations

import os
from pathlib import Path

from .adapters.db.facade import DB

_DEFAULT_URL = "sqlite:///./penny.db"

_db: DB | None = None
_readonly_db: DB | None = None


def normalize_database_url(raw: str) -> str:
    """Normalize a user-supplied database URL.

    - A bare filesystem path (``~/penny.db``, ``./data/penny.db``) becomes a
      SQLite URL.
    - ``postgres://`` (the scheme Heroku/older providers hand out, which
      SQLAlchemy 2 rejects) becomes ``postgresql://``.
    - Anything else passes through untouched — an undialable URL should fail
      loudly in SQLAlchemy's words, not be second-guessed here.
    """
    url = raw.strip()
    if not url:
        return url
    if "://" not in url:
        return f"sqlite:///{Path(url).expanduser()}"
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def resolve_database_url() -> str:
    """The configured database URL: ``PENNY_DATABASE_URL`` > ``DATABASE_URL``."""
    raw = (
        os.environ.get("PENNY_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
        or _DEFAULT_URL
    )
    return normalize_database_url(raw)


def get_db() -> DB:
    global _db
    if _db is None:
        url = resolve_database_url()
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
