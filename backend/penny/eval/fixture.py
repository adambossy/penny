"""Snapshot prod finance data into a writable SQLite copy; build / hydrate the
per-run eval fixture.

The eval replays the categorizer against a *writable, throwaway* copy of prod
finance data. That copy is a local SQLite file built by `snapshot_to_sqlite`
(and `snapshot_finance_to_sqlite` for the RLS-scoped read-only path) — no Neon
branch, no control-plane credential. At the end of a run the same SQLite is
bundled into a fixture and uploaded to R2 so a backtest can replay the exact
frozen input + history.

The fixture is a gzipped tar bundling two things, because the categorizer reads
*both* the DB and one workspace file:

- ``fixture.sqlite`` — the categorizer-reachable tables, copied table-by-table
  through the shared SQLAlchemy models (NOT pg_dump, so types/constraints stay
  dialect-correct).
- ``merchant-rules.md`` — the merchant-rules prompt block (``~/.transactoid/
  memory/merchant-rules.md``). Snapshotting it makes backtests reproducible:
  without it a backtest would run against *today's* rules, not the run's.

SQLite is dev-only in Penny, so backtests carry a minor fidelity caveat vs. the
Postgres run — acceptable for a non-production backtest.
"""

from __future__ import annotations

import gzip
import io
from pathlib import Path
import tarfile
import tempfile

from sqlalchemy import insert, select

from penny.adapters.db.facade import DB
from penny.adapters.db.models import (
    Category,
    DerivedTransaction,
    Merchant,
    PlaidItem,
    PlaidTransaction,
    Tag,
    TransactionCategoryEvent,
    TransactionTag,
)

# Categorizer-reachable tables + their referential closure, in FK-safe insert
# order. (FK enforcement is off on the fixture DB, but a sensible order keeps the
# file clean and makes the closure explicit.)
_FIXTURE_MODELS = (
    Category,
    Merchant,
    PlaidItem,
    PlaidTransaction,
    DerivedTransaction,
    TransactionCategoryEvent,
    Tag,
    TransactionTag,
)

_SQLITE_MEMBER = "fixture.sqlite"
_RULES_MEMBER = "merchant-rules.md"


def _copy_tables(src_db: DB, dst_db: DB, *, on_conflict_ignore: bool = False) -> None:
    """Copy every ``_FIXTURE_MODELS`` table from ``src_db`` into ``dst_db``.

    ``on_conflict_ignore`` uses SQLite's ``INSERT OR IGNORE`` so the same row may
    be copied more than once without a primary-key collision — needed by the
    principal-union snapshot, where a ``shared`` row surfaces under several
    tenants' RLS-scoped views (``dst_db`` is always SQLite).
    """
    for model in _FIXTURE_MODELS:
        table = model.__table__
        with src_db.session() as session:
            rows = [
                dict(row) for row in session.execute(select(table)).mappings().all()
            ]
        if rows:
            stmt = insert(table)
            if on_conflict_ignore:
                stmt = stmt.prefix_with("OR IGNORE")
            with dst_db.session() as session:
                session.execute(stmt, rows)


def snapshot_to_sqlite(src_db: DB, sqlite_path: str | Path) -> DB:
    """Copy ``src_db``'s finance closure into a fresh writable SQLite at ``sqlite_path``.

    Returns the open SQLite ``DB``. This is the neonctl-free replacement for a
    disposable Neon branch: a writable copy the eval can replay (and write) on
    without touching prod. Reads whatever ``src_db`` exposes.
    """
    dst_db = DB(f"sqlite:///{sqlite_path}", enforce_sqlite_fks=False)
    dst_db.create_schema()
    _copy_tables(src_db, dst_db)
    return dst_db


def snapshot_finance_to_sqlite(readonly_db: DB, sqlite_path: str | Path) -> DB:
    """Snapshot the finance closure through the read-only handle into SQLite."""
    dst_db = DB(f"sqlite:///{sqlite_path}", enforce_sqlite_fks=False)
    dst_db.create_schema()
    _copy_tables(readonly_db, dst_db)
    return dst_db


def _read_merchant_rules() -> str:
    """The active merchant-rules.md from the workspace ("" if absent)."""
    from penny.workspace import resolve_memory_dir

    path = resolve_memory_dir() / "merchant-rules.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _tar_add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def build_fixture_bytes(src_db: DB) -> bytes:
    """Bundle ``src_db``'s reachable tables + merchant-rules.md as gzipped tar bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_path = Path(tmp) / _SQLITE_MEMBER
        # Dispose so WAL checkpoints into the main file; the raw bytes below
        # must be the complete database.
        snapshot_to_sqlite(src_db, sqlite_path).dispose()
        sqlite_bytes = sqlite_path.read_bytes()
    rules_bytes = _read_merchant_rules().encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, _SQLITE_MEMBER, sqlite_bytes)
        _tar_add(tar, _RULES_MEMBER, rules_bytes)
    return buf.getvalue()


def hydrate_fixture(blob: bytes, workspace_dir: str | Path) -> DB:
    """Unpack a fixture into ``workspace_dir`` (a PENNY_WORKSPACE) and open the DB.

    Lays out ``<workspace_dir>/fixture.sqlite`` and
    ``<workspace_dir>/memory/merchant-rules.md`` so a backtest can set
    ``PENNY_WORKSPACE=<workspace_dir>`` and have the categorizer read the
    snapshotted rules. Accepts the legacy gzipped-SQLite format too (rules empty).
    """
    wd = Path(workspace_dir)
    (wd / "memory").mkdir(parents=True, exist_ok=True)
    sqlite_bytes: bytes
    rules_bytes = b""
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            sqlite_bytes = tar.extractfile(_SQLITE_MEMBER).read()  # type: ignore[union-attr]
            member = (
                tar.extractfile(_RULES_MEMBER)
                if _RULES_MEMBER in tar.getnames()
                else None
            )
            if member is not None:
                rules_bytes = member.read()
    except tarfile.ReadError:
        # Legacy format: a bare gzipped SQLite file (no bundled rules).
        sqlite_bytes = gzip.decompress(blob)
    sqlite_path = wd / _SQLITE_MEMBER
    sqlite_path.write_bytes(sqlite_bytes)
    (wd / "memory" / "merchant-rules.md").write_bytes(rules_bytes)
    return DB(f"sqlite:///{sqlite_path}", enforce_sqlite_fks=False)
