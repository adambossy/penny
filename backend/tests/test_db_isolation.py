"""The suite must never reach a developer's real database.

Both guards here failed silently once. ``penny init`` writes
``PENNY_DATABASE_URL`` into the workspace ``config.toml``; every entrypoint
applies that config as environment DEFAULTS at import; and
``resolve_database_url`` prefers ``PENNY_DATABASE_URL`` over ``DATABASE_URL``.
So on a machine where ``init`` had run against Postgres, the whole suite was
redirected at that database — and ``isolated_db``, which set only
``DATABASE_URL``, could not override it.

It surfaced as noisy failures rather than data loss only because ``create_all``
refuses to run on Postgres. Against a database without that guard, tests would
have written to it.
"""

from __future__ import annotations

import os

from penny.db import resolve_database_url
from penny.settings import config_path


def test_the_suite_does_not_load_a_developers_workspace_config() -> None:
    """The workspace is redirected before the app is imported, so a real
    config.toml on this machine is never read."""
    assert not config_path().exists(), (
        f"the suite is reading a real workspace config at {config_path()} — "
        "PENNY_WORKSPACE must be redirected in conftest before penny is imported"
    )


def test_isolated_db_sets_the_variable_that_actually_wins(isolated_db) -> None:
    """Setting only DATABASE_URL is not isolation: PENNY_DATABASE_URL wins."""
    assert os.environ["PENNY_DATABASE_URL"].startswith("sqlite:")
    assert resolve_database_url().startswith("sqlite:")


def test_isolated_db_resolves_away_from_any_ambient_postgres(isolated_db) -> None:
    """Whatever the ambient environment holds, the fixture's sqlite resolves."""
    resolved = resolve_database_url()
    assert resolved.startswith("sqlite:")
    assert "postgres" not in resolved
