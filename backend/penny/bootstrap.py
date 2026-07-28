"""First-run bootstrap: create tables, seed the taxonomy.

Idempotent. Safe to call on every backend startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session
import yaml

from .adapters.db.models import Category
from .db import get_db

_TAXONOMY_YAML = Path(__file__).resolve().parent.parent / "configs" / "taxonomy.yaml"


def bootstrap() -> None:
    """Ensure schema + seed the taxonomy.

    SQLite builds the schema from the models via ``create_all``. On Postgres
    the schema is owned by alembic and applied by ``penny migrate`` (run
    automatically at onboarding / server startup), so bootstrap creates
    nothing there — it only seeds.
    """
    # Importing the app-store models registers them on the shared Base so
    # create_all builds the whole single-player schema (finance + app_*).
    from .api.persistence import models as _app_models  # noqa: F401

    db = get_db()
    if db.dialect == "sqlite":
        db.create_schema()
    else:
        # REQUIREMENTS T3: never run the single-player app against the hosted
        # multi-tenant database — refuse before touching anything.
        import sqlalchemy as sa

        if "households" in sa.inspect(db._engine).get_table_names():
            from .schema import ForeignDatabaseError

            raise ForeignDatabaseError(
                "Refusing to start: this looks like a HOSTED multi-tenant "
                "Penny database (a 'households' table exists). Point "
                "PENNY_DATABASE_URL at a database of your own, or export "
                "your data with backend/transient/single-player-export."
            )
    with db.session() as session:
        seed_taxonomy(session)


def seed_taxonomy(session: Session) -> None:
    """Seed the YAML taxonomy if the database has no categories."""
    if not _TAXONOMY_YAML.exists():
        logger.warning(
            "Taxonomy YAML missing at {} — skipping seed. Run `uv run "
            "python scripts/sync_taxonomy_from_supabase.py` to fetch.",
            _TAXONOMY_YAML,
        )
        return

    existing = session.query(Category).count()
    if existing > 0:
        logger.debug("Database already has {} categories; skip seed.", existing)
        return

    raw = yaml.safe_load(_TAXONOMY_YAML.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        logger.error("taxonomy.yaml is not a list of category rows; got {}", type(raw))
        return

    # Two passes: parents first (so children's parent_id resolves).
    by_key: dict[str, Category] = {}
    for row in raw:
        if row.get("parent_key") is None:
            cat = _row_to_category(row, parent_id=None)
            session.add(cat)
            session.flush()
            by_key[cat.key] = cat
    for row in raw:
        parent_key = row.get("parent_key")
        if parent_key is None:
            continue
        parent = by_key.get(parent_key)
        if parent is None:
            logger.warning(
                "Skipping {!r} — parent {!r} not found", row.get("key"), parent_key
            )
            continue
        cat = _row_to_category(row, parent_id=parent.category_id)
        session.add(cat)
    session.flush()
    logger.info(
        "Seeded {} categories from {}",
        session.query(Category).count(),
        _TAXONOMY_YAML,
    )


def _row_to_category(row: dict[str, Any], *, parent_id: int | None) -> Category:
    return Category(
        key=row["key"],
        name=row["name"],
        parent_id=parent_id,
        description=row.get("description"),
        rules=row.get("rules"),
    )
