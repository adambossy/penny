"""Schema authority.

Alembic owns durable (Postgres) schemas; ``create_all`` owns ephemeral SQLite
(local dev, the test suite, the eval store). This module is the single
in-process entry point for applying migrations — used by ``penny
migrate``/``init``/``serve`` and the schema-drift test.

See ``docs/superpowers/plans/2026-07-09-alembic-sole-authority-on-postgres.md``
for the why: running ``create_all`` on a Postgres DB makes it a second, silent
schema authority that collides with alembic (the phase-3 cutover root cause).
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
import sqlalchemy as sa

from alembic import command
from penny.db import resolve_database_url


class ForeignDatabaseError(RuntimeError):
    """The target database is not a single-player Penny database."""


# alembic.ini lives beside the penny package in a source checkout (backend/),
# but in the deployed image the package is installed into site-packages while
# alembic.ini + db/migrations sit in the WORKDIR — so fall back to cwd. %(here)s
# in the ini resolves to the ini's own directory, so version_locations finds
# the version scripts from either location.
def _alembic_ini() -> Path:
    candidates = (
        Path(__file__).resolve().parent.parent / "alembic.ini",
        Path.cwd() / "alembic.ini",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "alembic.ini not found; looked in " + ", ".join(str(c) for c in candidates)
    )


def refuse_multi_tenant(engine: sa.Engine, *, action: str = "migrate") -> None:
    """Refuse to run the single-player app against a multi-tenant database.

    REQUIREMENTS T3: the hosted (multi-tenant) database must never receive
    core migrations past the 028 fork — migration 029 would destroy its
    tenancy. The tell is structural: a single-player database NEVER has a
    ``households`` table (fresh chains create it transiently mid-upgrade, but
    a database that already has one at rest is the hosted product's). The
    caller with a genuinely multi-tenant Penny DB migrates it with the
    penny-web tooling, or moves data here via the single-player export tool
    (backend/transient/single-player-export).

    The single source of the tell — both ``upgrade_to_head`` (migrate) and
    ``bootstrap`` (serve) call this. ``action`` names the refused operation
    in the error ("migrate", "start", ...).
    """
    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    if "households" not in tables:
        return
    version: str | None = None
    if "alembic_version" in tables:
        with engine.connect() as conn:
            version = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    raise ForeignDatabaseError(
        f"Refusing to {action}: this looks like a HOSTED multi-tenant Penny "
        f"database (a 'households' table exists; alembic_version={version!r}). "
        "Single-player migrations (029+) would destroy its tenancy. Point "
        "PENNY_DATABASE_URL at a database of your own, or export your data "
        "with backend/transient/single-player-export first."
    )


def upgrade_to_head(database_url: str | None = None) -> None:
    """Apply alembic migrations to head. Idempotent (no-op when already at head).

    A passed ``database_url`` is authoritative: ``env.py`` prefers the explicit
    ``sqlalchemy.url`` set here over ``DATABASE_URL``, so a caller can migrate a
    chosen DB even when a stray ``DATABASE_URL`` points elsewhere. With no
    argument the configured URL (``PENNY_DATABASE_URL``/``DATABASE_URL``) is
    used. Refuses multi-tenant targets (see :func:`refuse_multi_tenant`).
    """
    url = database_url or resolve_database_url()
    engine = sa.create_engine(url)
    try:
        refuse_multi_tenant(engine)
    finally:
        engine.dispose()
    cfg = Config(str(_alembic_ini()))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
