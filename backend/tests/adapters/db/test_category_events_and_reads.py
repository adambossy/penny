"""Tests for the categorization audit columns and read API.

Covers:
- reason routing in ``_insert_category_event`` (manual -> recategorization_reason,
  llm -> categorization_reasoning),
- the read methods used by the categorizer agent / chat agent.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from penny.adapters.db.facade import DB
from penny.adapters.db.models import (
    Category,
    DerivedTransaction,
    Merchant,
    PlaidTransaction,
    Tag,
    TransactionCategoryEvent,
    TransactionTag,
)


def _create_db(tmp_path: Path) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_schema()
    return db


def _seed_category(db: DB, key: str, name: str) -> int:
    with db.session() as session:
        cat = Category(key=key, name=name)
        session.add(cat)
        session.flush()
        return int(cat.category_id)


def _seed_merchant(db: DB, normalized: str, display: str) -> int:
    with db.session() as session:
        merchant = Merchant(normalized_name=normalized, display_name=display)
        session.add(merchant)
        session.flush()
        return int(merchant.merchant_id)


def _seed_txn(
    db: DB,
    *,
    external_id: str,
    descriptor: str,
    merchant_id: int | None = None,
    category_id: int | None = None,
    is_verified: bool = False,
) -> int:
    with db.session() as session:
        plaid = PlaidTransaction(
            external_id=f"plaid-{external_id}",
            source="PLAID",
            account_id="acct-1",
            item_id=None,
            posted_at=date(2026, 1, 10),
            amount_cents=5000,
            currency="USD",
        )
        session.add(plaid)
        session.flush()
        # The category provenance CHECK requires category_id, category_method and
        # category_assigned_at to be all-NULL or all-set together.
        has_category = category_id is not None
        txn = DerivedTransaction(
            plaid_transaction_id=plaid.plaid_transaction_id,
            external_id=external_id,
            amount_cents=5000,
            posted_at=date(2026, 1, 10),
            merchant_descriptor=descriptor,
            merchant_id=merchant_id,
            category_id=category_id,
            category_method="llm" if has_category else None,
            category_assigned_at=datetime(2026, 1, 10, 12, 0, 0)
            if has_category
            else None,
            is_verified=is_verified,
        )
        session.add(txn)
        session.flush()
        return int(txn.transaction_id)


def _events(db: DB) -> list[TransactionCategoryEvent]:
    with db.session() as session:
        rows = session.query(TransactionCategoryEvent).all()
        session.expunge_all()
        return rows


def test_manual_recat_writes_recategorization_reason(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    groceries = _seed_category(db, "food.groceries", "Groceries")
    restaurants = _seed_category(db, "food.restaurants", "Restaurants")
    merchant_id = _seed_merchant(db, "jubilee market", "Jubilee Market")
    _seed_txn(
        db,
        external_id="t1",
        descriptor="JUBILEE MARKET",
        merchant_id=merchant_id,
        category_id=restaurants,
    )

    updated = db.recategorize_merchant(
        merchant_id, groceries, reason="User says it's their grocer, not a restaurant"
    )

    assert updated == 1
    events = _events(db)
    assert len(events) == 1
    event = events[0]
    assert event.method == "manual"
    assert event.recategorization_reason == (
        "User says it's their grocer, not a restaurant"
    )
    assert event.categorization_reasoning is None
    assert event.to_category_key == "food.groceries"


def test_llm_categorization_writes_categorization_reasoning(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    groceries = _seed_category(db, "food.groceries", "Groceries")
    txn_id = _seed_txn(db, external_id="t1", descriptor="WHOLE FOODS")

    db.update_derived_mutable(
        txn_id,
        {
            "category_id": groceries,
            "category_method": "llm",
            "category_model": "gemini-3.6-flash",
            "category_reason": "Whole Foods is a grocery store.",
        },
    )

    events = _events(db)
    assert len(events) == 1
    event = events[0]
    assert event.method == "llm"
    assert event.categorization_reasoning == "Whole Foods is a grocery store."
    assert event.recategorization_reason is None


def test_recent_category_events_returns_dicts(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    groceries = _seed_category(db, "food.groceries", "Groceries")
    merchant_id = _seed_merchant(db, "jubilee market", "Jubilee Market")
    _seed_txn(
        db,
        external_id="t1",
        descriptor="JUBILEE MARKET",
        merchant_id=merchant_id,
        category_id=_seed_category(db, "food.restaurants", "Restaurants"),
    )
    db.recategorize_merchant(merchant_id, groceries, reason="grocer not restaurant")

    events = db.recent_category_events(limit=10)

    assert len(events) == 1
    assert events[0]["merchant_descriptor"] == "JUBILEE MARKET"
    assert events[0]["recategorization_reason"] == "grocer not restaurant"
    assert events[0]["to_category_key"] == "food.groceries"


def test_verified_category_for_descriptor(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    groceries = _seed_category(db, "food.groceries", "Groceries")
    # A verified row for this descriptor -> fast path can reuse it.
    _seed_txn(
        db,
        external_id="t1",
        descriptor="TRADER JOES #42",
        category_id=groceries,
        is_verified=True,
    )
    # An unverified row for a different descriptor must not match.
    _seed_txn(
        db,
        external_id="t2",
        descriptor="MYSTERY VENDOR",
        category_id=groceries,
        is_verified=False,
    )

    assert db.verified_category_for_descriptor("TRADER JOES #42") == "food.groceries"
    assert db.verified_category_for_descriptor("MYSTERY VENDOR") is None
    assert db.verified_category_for_descriptor("NEVER SEEN") is None


def test_tags_for_transactions(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    txn_id = _seed_txn(db, external_id="t1", descriptor="CAFE DE FLORE")
    with db.session() as session:
        tag = Tag(name="eurotrip")
        session.add(tag)
        session.flush()
        session.add(TransactionTag(transaction_id=txn_id, tag_id=tag.tag_id))

    mapping = db.tags_for_transactions([txn_id])
    assert mapping == {txn_id: ["eurotrip"]}

    transactions = db.get_transactions_by_tag("eurotrip")
    assert len(transactions) == 1
    assert transactions[0]["merchant_descriptor"] == "CAFE DE FLORE"
