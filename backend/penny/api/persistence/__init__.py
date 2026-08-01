"""App-owned persistence: conversations, reminders, onboarding.

This package owns the app-bookkeeping tables (``app_*`` prefix). They share
the single-player database and SQLAlchemy ``Base`` with the finance tables,
but the package keeps the domain seam: it imports neither ``penny.tools`` nor
``penny.agent_factory``, and agent code never imports it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def app_session() -> Iterator[Session]:
    """A commit-on-success session on the shared single-player engine."""
    from penny.db import get_db

    with get_db().session() as session:
        yield session
