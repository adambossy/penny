"""Bootstrap creates the schema and seeds the taxonomy exactly once."""

from __future__ import annotations

from penny.adapters.db.models import Category
from penny.bootstrap import bootstrap
from penny.db import get_db


def _category_count() -> int:
    with get_db().session() as session:
        return session.query(Category).count()


def test_bootstrap_seeds_taxonomy_idempotently(isolated_db):
    bootstrap()
    seeded = _category_count()

    bootstrap()
    output = _category_count()

    assert seeded > 0
    assert output == seeded
