"""Tests for sign-convention normalization in SyncTool._mutate_batch_to_derived."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from penny.adapters.db.facade import DB
from penny.adapters.db.models import DerivedTransaction, PlaidTransaction
from penny.tools._services.sync_service import SyncTool, _apply_sign_convention


def _create_db(tmp_path: Path) -> DB:
    """Create a file-backed SQLite DB with full schema and FK enforcement."""
    db = DB(f"sqlite:///{tmp_path / 'test.db'}", enforce_sqlite_fks=True)
    db.create_schema()
    return db


def _create_sync_tool(db: DB) -> SyncTool:
    """Create a SyncTool with mocked Plaid/categorizer/taxonomy deps."""
    return SyncTool(
        plaid_client=MagicMock(),
        categorizer_factory=MagicMock(),
        db=db,
        taxonomy=MagicMock(),
    )


def _insert_plaid_txn(
    db: DB,
    *,
    external_id: str,
    account_id: str,
    amount_cents: int,
    posted_at: date = date(2026, 3, 1),
    merchant_descriptor: str = "Test Merchant",
) -> int:
    """Insert a PlaidTransaction and return its PK."""
    with db.session() as session:
        plaid_txn = PlaidTransaction(
            external_id=external_id,
            source="PLAID",
            account_id=account_id,
            item_id=None,
            posted_at=posted_at,
            amount_cents=amount_cents,
            currency="USD",
            merchant_descriptor=merchant_descriptor,
        )
        session.add(plaid_txn)
        session.flush()
        plaid_id: int = plaid_txn.plaid_transaction_id
    return plaid_id


def _fetch_derived_amounts(db: DB, plaid_id: int) -> list[int]:
    """Return all derived amount_cents values for a given plaid_id, sorted."""
    with db.session() as session:
        rows = (
            session.query(DerivedTransaction)
            .filter_by(plaid_transaction_id=plaid_id)
            .all()
        )
        for row in rows:
            session.expunge(row)
    return sorted(row.amount_cents for row in rows)


def _fetch_plaid_amount(db: DB, plaid_id: int) -> int:
    """Return the amount_cents stored in plaid_transactions for a given PK."""
    txns = db.get_plaid_transactions_by_ids([plaid_id])
    return txns[plaid_id].amount_cents


def _fetch_derived_rows(db: DB, plaid_id: int) -> list[DerivedTransaction]:
    """Return all DerivedTransaction rows for a plaid_id, detached from session."""
    with db.session() as session:
        rows = (
            session.query(DerivedTransaction)
            .filter_by(plaid_transaction_id=plaid_id)
            .all()
        )
        for row in rows:
            session.expunge(row)
    return rows


@pytest.mark.parametrize(
    "sign_convention,amount_cents,expected_cents",
    [
        ("expense_positive", 4500, 4500),
        ("expense_negative", 4500, -4500),
    ],
)
def test_apply_sign_convention(
    sign_convention: str, amount_cents: int, expected_cents: int
) -> None:
    """_apply_sign_convention flips for expense_negative, no-op for expense_positive."""
    # input — real transient ORM instance (exercises copy + make_transient path)
    plaid_txn = PlaidTransaction(
        external_id="txn-helper-test",
        source="PLAID",
        account_id="acct-test",
        item_id=None,
        posted_at=date(2026, 1, 1),
        amount_cents=amount_cents,
        currency="USD",
    )

    # act
    view = _apply_sign_convention(plaid_txn, sign_convention=sign_convention)

    # assert
    assert view.amount_cents == expected_cents


def test_apply_sign_convention_does_not_mutate_original() -> None:
    """The original PlaidTransaction instance is never mutated by the helper."""
    # input
    amount_cents = 4500
    plaid_txn = PlaidTransaction(
        external_id="txn-immut",
        source="PLAID",
        account_id="acct-immut",
        item_id=None,
        posted_at=date(2026, 1, 1),
        amount_cents=amount_cents,
        currency="USD",
    )

    # act
    _apply_sign_convention(plaid_txn, sign_convention="expense_negative")

    # expected / assert — original unchanged
    assert plaid_txn.amount_cents == amount_cents


def test_mutate_batch_expense_positive_derived_amount_unchanged(
    tmp_path: Path,
) -> None:
    """expense_positive account -> derived amount_cents matches plaid_txn."""
    # input
    account_id = "acct-pos"
    bank_amount_cents = 5000

    # setup
    db = _create_db(tmp_path)
    db.set_sign_convention(account_id, "expense_positive")
    plaid_id = _insert_plaid_txn(
        db, external_id="pos-1", account_id=account_id, amount_cents=bank_amount_cents
    )
    sync_tool = _create_sync_tool(db)

    # act
    sync_tool._mutate_batch_to_derived([plaid_id])

    # expected
    expected_output = [bank_amount_cents]

    # assert
    assert _fetch_derived_amounts(db, plaid_id) == expected_output


def test_mutate_batch_expense_negative_derived_amount_negated(
    tmp_path: Path,
) -> None:
    """expense_negative account -> derived amount_cents is negated (canonical sign)."""
    # input
    account_id = "acct-neg"
    bank_amount_cents = -5000  # expenses are negative in this bank's convention

    # setup
    db = _create_db(tmp_path)
    db.set_sign_convention(account_id, "expense_negative")
    plaid_id = _insert_plaid_txn(
        db, external_id="neg-1", account_id=account_id, amount_cents=bank_amount_cents
    )
    sync_tool = _create_sync_tool(db)

    # act
    sync_tool._mutate_batch_to_derived([plaid_id])

    # expected — flip sign to canonical positive-expense
    expected_output = [-bank_amount_cents]

    # assert
    assert _fetch_derived_amounts(db, plaid_id) == expected_output


def test_mutate_batch_no_convention_row_defaults_to_expense_positive(
    tmp_path: Path,
) -> None:
    """Account with no convention row falls back to expense_positive with a WARNING."""
    from loguru import logger

    # input
    account_id = "acct-unknown"
    bank_amount_cents = 3000

    # setup — no call to set_sign_convention, so no row exists
    db = _create_db(tmp_path)
    plaid_id = _insert_plaid_txn(
        db,
        external_id="unknown-1",
        account_id=account_id,
        amount_cents=bank_amount_cents,
    )
    sync_tool = _create_sync_tool(db)

    # Capture loguru warnings via a temporary in-memory sink
    captured_messages: list[str] = []

    def _capture_sink(message: object) -> None:
        captured_messages.append(str(message))

    sink_id = logger.add(_capture_sink, level="WARNING", format="{message}")

    try:
        # act
        sync_tool._mutate_batch_to_derived([plaid_id])
    finally:
        logger.remove(sink_id)

    # expected — no flip; amount passes through unchanged
    expected_output = [bank_amount_cents]

    # assert
    assert _fetch_derived_amounts(db, plaid_id) == expected_output
    # A WARNING must have been emitted mentioning the unknown account_id
    assert any(account_id in msg for msg in captured_messages), (
        f"Expected WARNING mentioning {account_id!r}; got: {captured_messages}"
    )


def test_mutate_batch_plaid_transaction_immutability(
    tmp_path: Path,
) -> None:
    """After normalization the original plaid_transactions row carries bank amount."""
    # input
    account_id = "acct-imm"
    bank_amount_cents = -7500  # bank reports expenses as negative

    # setup
    db = _create_db(tmp_path)
    db.set_sign_convention(account_id, "expense_negative")
    plaid_id = _insert_plaid_txn(
        db, external_id="imm-1", account_id=account_id, amount_cents=bank_amount_cents
    )
    sync_tool = _create_sync_tool(db)

    # act — normalization must NOT write back to plaid_transactions
    sync_tool._mutate_batch_to_derived([plaid_id])

    # expected — bank original is preserved
    expected_output = bank_amount_cents

    # assert
    assert _fetch_plaid_amount(db, plaid_id) == expected_output


def test_mutate_batch_skips_rederive_when_verified_row_exists(
    tmp_path: Path,
) -> None:
    """An identical verified transaction is a no-op, not a conflict."""
    from penny.tools._services.mutation_reconciliation import MutationReport

    # input
    account_id = "acct-verified"
    bank_amount_cents = -8000  # expense_negative convention

    # setup
    db = _create_db(tmp_path)
    db.set_sign_convention(account_id, "expense_negative")
    plaid_id = _insert_plaid_txn(
        db,
        external_id="verified-1",
        account_id=account_id,
        amount_cents=bank_amount_cents,
    )

    # First sync: creates derived row with bank-sign amount
    sync_tool = _create_sync_tool(db)
    sync_tool._mutate_batch_to_derived([plaid_id])

    # Mark the derived row as verified (simulates user verification pre-sign-convention)
    rows = _fetch_derived_rows(db, plaid_id)
    assert len(rows) == 1
    with db.session() as session:
        row = session.get(DerivedTransaction, rows[0].transaction_id)
        assert row is not None
        row.is_verified = True

    original_amount = _fetch_derived_amounts(db, plaid_id)[0]

    report = MutationReport()
    sync_tool._mutate_batch_to_derived([plaid_id], report=report)
    assert _fetch_derived_amounts(db, plaid_id) == [original_amount]
    assert report.unchanged == [plaid_id]
    assert report.conflicts == []


def test_mutate_batch_investment_expense_negative_derived_negated(
    tmp_path: Path,
) -> None:
    """Investment transaction at expense_negative account gets negated in derived."""
    # input
    account_id = "acct-inv-neg"
    bank_amount_cents = -12000  # bank reports investment buy as negative

    # setup
    db = _create_db(tmp_path)
    db.set_sign_convention(account_id, "expense_negative")
    plaid_id = _insert_plaid_txn(
        db,
        external_id="inv-neg-1",
        account_id=account_id,
        amount_cents=bank_amount_cents,
        merchant_descriptor="Schwab Investment",
    )
    sync_tool = _create_sync_tool(db)

    # act
    sync_tool._mutate_batch_to_derived([plaid_id])

    # expected — canonical sign is positive
    expected_output = [-bank_amount_cents]

    # assert
    assert _fetch_derived_amounts(db, plaid_id) == expected_output


def _seed_amazon_order(
    db: DB,
    *,
    order_id: str,
    order_total_cents: int,
    item_price_cents: list[int],
) -> None:
    """Seed one Amazon order with items of given price_cents values."""
    profile = db.create_amazon_login_profile(
        profile_key="primary", display_name="Primary"
    )
    db.upsert_amazon_order(
        order_id=order_id,
        order_date=date(2026, 3, 1),
        order_total_cents=order_total_cents,
        tax_cents=0,
        shipping_cents=0,
        profile_id=profile.profile_id,
    )
    for idx, price_cents in enumerate(item_price_cents):
        db.upsert_amazon_item(
            order_id=order_id,
            asin=f"B{idx:03d}",
            description=f"Item {idx}",
            price_cents=price_cents,
            quantity=1,
        )


def test_mutate_batch_amazon_expense_negative_items_sum_to_canonical(
    tmp_path: Path,
) -> None:
    """Amazon split with expense_negative: item amounts sum to normalized total."""
    # input
    account_id = "acct-amz-neg"
    # Bank reports expenses as negative; the order total in Amazon is positive $60
    bank_amount_cents = -6000
    item_prices = [2000, 2500, 1500]  # sums to 6000

    # setup
    db = _create_db(tmp_path)
    db.set_sign_convention(account_id, "expense_negative")
    order_id = "113-1111111-9999999"
    _seed_amazon_order(
        db,
        order_id=order_id,
        order_total_cents=abs(bank_amount_cents),
        item_price_cents=item_prices,
    )
    plaid_id = _insert_plaid_txn(
        db,
        external_id="amz-neg-1",
        account_id=account_id,
        amount_cents=bank_amount_cents,
        posted_at=date(2026, 3, 5),  # within max_date_lag=30 of order_date
        merchant_descriptor="AMAZON.COM",
    )
    sync_tool = _create_sync_tool(db)

    # act
    sync_tool._mutate_batch_to_derived([plaid_id])

    # expected — normalized total is +6000; items must sum to +6000 and each be positive
    canonical_total = -bank_amount_cents  # 6000
    derived_amounts = _fetch_derived_amounts(db, plaid_id)

    # assert — items sum to canonical total
    assert sum(derived_amounts) == canonical_total
    # each individual item should be positive (canonical expense direction)
    assert all(amt > 0 for amt in derived_amounts), (
        f"Expected all item amounts > 0 (expense_positive), got: {derived_amounts}"
    )


def test_mutate_batch_amazon_expense_positive_items_sum_unchanged(
    tmp_path: Path,
) -> None:
    """Amazon split with expense_positive: item amounts sum to bank amount (no flip)."""
    # input
    account_id = "acct-amz-pos"
    bank_amount_cents = 6000
    item_prices = [2000, 2500, 1500]  # sums to 6000

    # setup
    db = _create_db(tmp_path)
    db.set_sign_convention(account_id, "expense_positive")
    order_id = "113-2222222-8888888"
    _seed_amazon_order(
        db,
        order_id=order_id,
        order_total_cents=bank_amount_cents,
        item_price_cents=item_prices,
    )
    plaid_id = _insert_plaid_txn(
        db,
        external_id="amz-pos-1",
        account_id=account_id,
        amount_cents=bank_amount_cents,
        posted_at=date(2026, 3, 5),
        merchant_descriptor="AMAZON.COM",
    )
    sync_tool = _create_sync_tool(db)

    # act
    sync_tool._mutate_batch_to_derived([plaid_id])

    # expected — no flip; items must sum to bank_amount_cents
    derived_amounts = _fetch_derived_amounts(db, plaid_id)

    # assert
    assert sum(derived_amounts) == bank_amount_cents
    assert all(amt > 0 for amt in derived_amounts)
