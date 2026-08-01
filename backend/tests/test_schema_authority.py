"""Schema-authority guarantees: alembic owns Postgres; create_all owns SQLite.

See docs/superpowers/plans/2026-07-09-alembic-sole-authority-on-postgres.md.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, inspect

from penny.adapters.db.facade import DB
from penny.schema import upgrade_to_head


def test_upgrade_to_head_builds_full_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    upgrade_to_head(url)

    insp = inspect(create_engine(url))
    tables = insp.get_table_names()
    assert "plaid_transactions" in tables
    assert "categories" in tables

    # Idempotent: a second run is a no-op, not an error.
    upgrade_to_head(url)


def test_explicit_url_wins_over_env_database_url(tmp_path, monkeypatch):
    """The url passed to upgrade_to_head must win over a stray DATABASE_URL, so
    `upgrade_to_head("sqlite:///tmp")` can never migrate a prod DB named in the
    environment. Guards the env.py precedence (explicit sqlalchemy.url first)."""
    target = f"sqlite:///{tmp_path / 'target.db'}"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'poison.db'}")

    upgrade_to_head(target)

    # The explicit target got the schema; the env-named DB was never touched.
    assert "categories" in inspect(create_engine(target)).get_table_names()
    assert not (tmp_path / "poison.db").exists()


def test_create_schema_refuses_postgres():
    # Constructed, never connected — the guard fires on the dialect name alone.
    db = DB("postgresql://u:p@localhost:5432/nope")
    with pytest.raises(RuntimeError, match="alembic"):
        db.create_schema()


class _FakeDB:
    def __init__(self, dialect: str, calls: list[str]) -> None:
        self.dialect = dialect
        self._calls = calls
        # The T3 foreign-DB guard inspects the engine for a households table;
        # a real in-memory engine keeps the fake honest (and tenant-free).
        from sqlalchemy import create_engine

        self.engine = create_engine("sqlite://")

    def create_schema(self) -> None:
        self._calls.append("finance")

    @contextmanager
    def session(self):
        yield None


def test_bootstrap_skips_create_all_on_postgres(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr("penny.bootstrap.get_db", lambda: _FakeDB("postgresql", calls))
    monkeypatch.setattr("penny.bootstrap.seed_taxonomy", lambda session: None)

    from penny.bootstrap import bootstrap

    bootstrap()
    assert calls == []  # Postgres schema is alembic-owned; no create_all


def test_bootstrap_creates_all_on_sqlite(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr("penny.bootstrap.get_db", lambda: _FakeDB("sqlite", calls))
    monkeypatch.setattr("penny.bootstrap.seed_taxonomy", lambda session: None)

    from penny.bootstrap import bootstrap

    bootstrap()
    assert calls == ["finance"]  # one schema now covers finance + app_ tables
