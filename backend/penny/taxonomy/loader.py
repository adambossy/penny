"""Taxonomy loader - handles database integration for taxonomy.

This module breaks the circular dependency between Taxonomy and DB by providing
standalone functions that coordinate between them.
"""

from __future__ import annotations

from penny.adapters.db.facade import DB
from penny.taxonomy.core import CategoryNode, Taxonomy


def load_taxonomy_from_db(db: DB) -> Taxonomy:
    """Load taxonomy from database.

    Args:
        db: Database facade instance

    Returns:
        Taxonomy instance populated from database
    """
    rows = db.fetch_categories()
    nodes: list[CategoryNode] = []
    for row in rows:
        nodes.append(
            CategoryNode(
                key=str(row["key"]),
                name=str(row["name"]),
                description=(
                    None if row.get("description") is None else str(row["description"])
                ),
                parent_key=(
                    None if row.get("parent_key") is None else str(row["parent_key"])
                ),
            )
        )
    # Sort to keep stable order
    nodes.sort(key=lambda n: n.key)
    return Taxonomy.from_nodes(nodes)


_category_id_cache: dict[str, int | None] = {}


def get_category_id(db: DB, taxonomy: Taxonomy, key: str) -> int | None:
    """Get category ID for a key, with in-process caching.

    Category keys are stable for the process lifetime, so a plain per-process
    cache suffices.

    Args:
        db: Database facade instance
        taxonomy: Taxonomy instance (unused, but kept for API compatibility)
        key: Category key

    Returns:
        Category ID or None if not found
    """
    if key in _category_id_cache:
        return _category_id_cache[key]
    category_id = db.get_category_id_by_key(key)
    _category_id_cache[key] = category_id
    return category_id


def clear_category_id_cache() -> None:
    """Clear the in-process category ID cache.

    Useful after taxonomy changes or in tests.
    """
    _category_id_cache.clear()
