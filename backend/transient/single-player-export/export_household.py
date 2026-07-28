"""Export one household from the hosted Postgres into a single-player SQLite DB.

Transient one-off (non-canonical; see README.md). Reads the multi-tenant
source (migration-028 shape) READ-ONLY, filters to ``--household-id``, strips
the tenant columns, and writes a fresh SQLite file shaped by the CURRENT
single-player models (imported read-only from the canonical package).

Primary keys are preserved verbatim (fresh destination, so no collisions),
which keeps every FK edge intact without remapping.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import sqlalchemy as sa

from penny.adapters.db.facade import DB
from penny.adapters.db.models import Base

# Source tables to copy, in FK-safe insert order, with the WHERE strategy:
#   household — filter by household_id (column stripped on write)
#   owner_vis — filter by household_id (household/owner/visibility stripped)
#   all       — copy every row (global tables)
#   merchants — only merchants referenced by the household's transactions
_TABLES: list[tuple[str, str]] = [
    ("categories", "household"),
    ("merchants", "merchants"),
    ("plaid_items", "household"),
    ("plaid_accounts", "owner_vis"),
    ("plaid_transactions", "owner_vis"),
    ("derived_transactions", "owner_vis"),
    ("transaction_category_events", "household"),
    ("tags", "household"),
    ("transaction_tags", "owner_vis"),
    ("amazon_login_profiles", "owner_vis"),
    ("amazon_orders", "owner_vis"),
    ("amazon_items", "owner_vis"),
    ("transaction_items", "owner_vis"),
    ("email_receipts", "owner_vis"),
    ("pending_receipt_matches", "owner_vis"),
    ("account_sign_conventions", "owner_vis"),
    ("eval_runs", "eval_runs"),
    ("eval_items", "all"),
]

_TENANT_COLUMNS = {"household_id", "owner_user_id", "visibility"}


def _dest_columns(table: str) -> set[str] | None:
    """Column names the current single-player model declares for ``table``."""
    dest = Base.metadata.tables.get(table)
    return None if dest is None else {c.name for c in dest.columns}


def _select_rows(
    src: sa.engine.Connection, table: str, mode: str, household_id: str
) -> list[dict]:
    if mode == "merchants":
        query = sa.text(
            "SELECT m.* FROM merchants m WHERE m.merchant_id IN ("
            "  SELECT DISTINCT merchant_id FROM derived_transactions"
            "  WHERE household_id = :hh AND merchant_id IS NOT NULL)"
        )
        params = {"hh": household_id}
    elif mode in ("household", "owner_vis"):
        query = sa.text(f"SELECT * FROM {table} WHERE household_id = :hh")  # noqa: S608
        params = {"hh": household_id}
    elif mode == "eval_runs":
        # eval_runs.household_id is nullable in the source; take the
        # household's runs plus legacy unscoped rows.
        query = sa.text(
            "SELECT * FROM eval_runs WHERE household_id = :hh OR household_id IS NULL"
        )
        params = {"hh": household_id}
    else:  # all
        query = sa.text(f"SELECT * FROM {table}")  # noqa: S608
        params = {}
    return [dict(row._mapping) for row in src.execute(query, params)]


def export(source_url: str, household_id: str, dest_path: Path) -> None:
    if dest_path.exists() and dest_path.stat().st_size > 0:
        sys.exit(f"refusing to write into existing non-empty {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    dest_db = DB(f"sqlite:///{dest_path}", enforce_sqlite_fks=False)
    # Register the app_* tables on the shared Base before create_all.
    from penny.api.persistence import models as _app_models  # noqa: F401

    dest_db.create_schema()

    src_engine = sa.create_engine(source_url)
    copied: dict[str, int] = {}
    with src_engine.connect() as src, dest_db.session() as dst:
        src_tables = set(sa.inspect(src_engine).get_table_names())
        for table, mode in _TABLES:
            if table not in src_tables:
                print(f"  {table}: absent in source, skipped")
                continue
            dest_cols = _dest_columns(table)
            if dest_cols is None:
                print(f"  {table}: absent in destination models, skipped")
                continue
            rows = _select_rows(src, table, mode, household_id)
            slim = [
                {k: v for k, v in row.items() if k in dest_cols} for row in rows
            ]
            if slim:
                dst.execute(sa.insert(Base.metadata.tables[table]), slim)
            copied[table] = len(slim)
            print(f"  {table}: {len(slim)} rows")
    dest_db.dispose()

    total = sum(copied.values())
    print(f"Exported {total} rows into {dest_path}")
    print(
        "Reminder: if the source encrypted Plaid tokens, set the same "
        "PENNY_PLAID_TOKEN_KEY in ~/.penny/config.toml [env]."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Source Postgres URL (read-only)")
    parser.add_argument("--household-id", required=True, help="Household UUID to export")
    parser.add_argument("--dest", required=True, help="Destination SQLite file path")
    args = parser.parse_args()
    export(args.source, args.household_id, Path(args.dest).expanduser())


if __name__ == "__main__":
    main()
