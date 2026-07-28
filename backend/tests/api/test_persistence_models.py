"""The app tables live on the shared finance Base, namespaced by app_."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from penny.adapters.db.models import Base
from penny.api.persistence import models as _app_models  # noqa: F401

_APP_TABLES = {
    "app_conversations",
    "app_conversation_messages",
    "app_queued_reminders",
    "app_onboarding_items",
}


def test_app_tables_register_on_shared_base():
    # Single-player: one Base, one database. The app tables are distinguished
    # from the finance tables only by their app_ prefix.
    assert _APP_TABLES <= set(Base.metadata.tables)


def test_create_all_builds_app_tables_on_sqlite(tmp_path: Path):
    # input: a fresh SQLite engine
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")

    # act: build the whole single-player schema from the models
    Base.metadata.create_all(engine)

    # expected: the app tables exist beside the finance tables
    table_names = set(inspect(engine).get_table_names())
    assert _APP_TABLES <= table_names
    assert "derived_transactions" in table_names


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
