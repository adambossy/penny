"""Round-trip the SQLite fixture: build from a source DB, hydrate, compare."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from penny.adapters.db.facade import DB
from penny.adapters.db.models import (
    Category,
    DerivedTransaction,
    PlaidTransaction,
    TransactionCategoryEvent,
)
from penny.eval.fixture import build_fixture_bytes, hydrate_fixture


def _seed_source(tmp_path: Path) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'src.db'}")
    db.create_schema()
    with db.session() as session:
        cat = Category(key="food.groceries", name="Groceries")
        session.add(cat)
        session.flush()
        plaid = PlaidTransaction(
            external_id="p1",
            source="PLAID",
            account_id="acct-1",
            item_id=None,
            posted_at=date(2026, 1, 10),
            amount_cents=5000,
            currency="USD",
        )
        session.add(plaid)
        session.flush()
        txn = DerivedTransaction(
            plaid_transaction_id=plaid.plaid_transaction_id,
            external_id="d1",
            amount_cents=5000,
            posted_at=date(2026, 1, 10),
            merchant_descriptor="WHOLE FOODS",
            category_id=cat.category_id,
            category_method="llm",
            category_assigned_at=datetime(2026, 1, 10, 12, 0),
        )
        session.add(txn)
        session.flush()
        session.add(
            TransactionCategoryEvent(
                transaction_id=txn.transaction_id,
                from_category_id=None,
                to_category_id=cat.category_id,
                from_category_key=None,
                to_category_key="food.groceries",
                method="llm",
                model="gemini-3.6-flash",
                categorization_reasoning="grocery store",
                created_at=datetime(2026, 1, 10, 12, 0),
            )
        )
    return db


def test_fixture_roundtrip(tmp_path: Path, monkeypatch) -> None:
    src = _seed_source(tmp_path)

    # A merchant-rules.md in the active workspace must be snapshotted into the
    # fixture so backtests are reproducible against the run's rules.
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / "merchant-rules.md").write_text("WHOLE FOODS -> food.groceries\n")
    monkeypatch.setenv("PENNY_WORKSPACE", str(ws))

    blob = build_fixture_bytes(src)
    assert isinstance(blob, bytes) and len(blob) > 0

    out_ws = tmp_path / "hydrated"
    hydrated = hydrate_fixture(blob, out_ws)
    # Reachable rows survived the round-trip.
    with hydrated.session() as session:
        assert session.query(DerivedTransaction).count() == 1
        assert session.query(Category).count() == 1
        assert session.query(TransactionCategoryEvent).count() == 1
        txn = session.query(DerivedTransaction).one()
        assert txn.merchant_descriptor == "WHOLE FOODS"

    # The categorizer read API works against the hydrated fixture (backtest path).
    assert hydrated.events_for_transaction(1)[0]["to_category_key"] == "food.groceries"

    # merchant-rules.md was bundled and laid out for PENNY_WORKSPACE=out_ws.
    rules = (out_ws / "memory" / "merchant-rules.md").read_text()
    assert rules == "WHOLE FOODS -> food.groceries\n"
