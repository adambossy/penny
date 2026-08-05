from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import tempfile

import pytest

# Sentry defaults on (project DSN is baked in); disable it for the suite so
# importing the app never initializes error reporting and test-triggered
# exceptions can't leak to the prod Sentry project. Set before importing
# penny.api.main below, which calls init_sentry() at import.
os.environ.setdefault("PENNY_SENTRY_ENABLED", "false")

# Point the workspace at a throwaway directory so the suite never loads the
# developer's real config.toml. `penny init` writes PENNY_DATABASE_URL there,
# and every entrypoint applies that config as environment DEFAULTS at import —
# so on a machine where init has run, importing the app below would set the
# developer's database (possibly production Postgres) before any fixture gets
# a chance to redirect it. `isolated_db` could not undo that: it sets
# DATABASE_URL, while the config sets PENNY_DATABASE_URL, which wins.
#
# Same reason and same placement as the Sentry line above: it must happen
# before `import penny.api.main`, which applies the config at import.
os.environ.setdefault(
    "PENNY_WORKSPACE", tempfile.mkdtemp(prefix="penny-test-workspace-")
)

import penny.api.main
import penny.db
import penny.observability.otel as _otel
import penny.services
import penny.taxonomy.loader


@pytest.fixture(autouse=True)
def _no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-disable Langfuse/OTEL tracing for every test.

    Tracing turns on automatically whenever ``LANGFUSE_PUBLIC_KEY`` +
    ``LANGFUSE_SECRET_KEY`` are present (e.g. exported into the shell, or
    sourced from ``.env.test``), so a bare ``pytest`` run would otherwise ship
    synthetic spans to the real Langfuse project. Force the explicit-off flag
    and pin the cached enablement decision so no test — fixture or scripted
    fake agent — can emit a trace.
    """
    monkeypatch.setenv("PENNY_LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(_otel, "_enabled", False)
    monkeypatch.setattr(_otel, "_provider", None)


def _reset_singletons() -> None:
    penny.db._db = None
    penny.db._readonly_db = None
    penny.services._taxonomy = None
    penny.services._rules_loader = None
    penny.services._persister = None
    penny.services._migrator = None
    penny.taxonomy.loader._category_id_cache.clear()


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the process-wide single-player DB at a fresh tmp SQLite file.

    Sets BOTH names deliberately. ``resolve_database_url`` prefers
    ``PENNY_DATABASE_URL`` over ``DATABASE_URL``, so setting only the latter
    leaves the fixture unable to override anything that set the former —
    which is exactly what a workspace ``config.toml`` does.
    """
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("PENNY_DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL", url)
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the workspace at a tmp dir so tests never touch the real one."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PENNY_WORKSPACE", str(workspace))
    return workspace
