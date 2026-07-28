"""The T3 guard: single-player migrations/serving refuse a multi-tenant DB.

Born from a real incident: a dev .env carrying the hosted DATABASE_URL let
``penny serve`` attempt the destructive fork migration (029) against the
hosted database (the transaction rolled back, but only by luck of a
constraint failure). The structural tell is the ``households`` table — no
single-player database has one at rest.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from penny.schema import ForeignDatabaseError, upgrade_to_head


def _make_tenantish_db(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'hosted.db'}"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE households (household_id TEXT PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num TEXT)"))
        conn.execute(
            sa.text(
                "INSERT INTO alembic_version VALUES ('028_add_eval_runs_household_id')"
            )
        )
    engine.dispose()
    return url


def test_upgrade_refuses_multi_tenant_database(tmp_path):
    url = _make_tenantish_db(tmp_path)
    with pytest.raises(ForeignDatabaseError, match="multi-tenant"):
        upgrade_to_head(url)
    # And nothing moved: still stamped where it was.
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        version = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert version == "028_add_eval_runs_household_id"


def test_upgrade_still_works_on_fresh_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    upgrade_to_head(url)  # builds the whole chain including 029 — no raise
    engine = sa.create_engine(url)
    tables = set(sa.inspect(engine).get_table_names())
    assert "households" not in tables
    assert "app_conversations" in tables
