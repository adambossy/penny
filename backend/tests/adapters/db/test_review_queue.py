"""The review queue and the scoreboard it feeds.

The point of the queue is that human labels are the *only* ground truth: the
sync-time categorizer and the eval's replay are one code path, so scoring one
against the other measures agreement with itself. These tests pin the two
properties that keeps honest — ``reviewed_at`` marks only human labels (the
fast path's auto-verify does not), and the scoreboard scores a row against
what the model chose on its own, read back from the event log after the human
overwrote the category.
"""

from __future__ import annotations

from datetime import date

import pytest

from penny.adapters.db.models import (
    Category,
    DerivedTransaction,
    PlaidTransaction,
)
from penny.db import get_db


def _category(key: str, name: str) -> int:
    with get_db().session() as session:
        row = Category(key=key, name=name)
        session.add(row)
        session.flush()
        category_id: int = row.category_id
        session.expunge(row)
    return category_id


def _txn(
    ext: str,
    *,
    category_id: int | None = None,
    method: str | None = "llm",
    descriptor: str = "Test Merchant",
    reporting_mode: str | None = None,
    is_hidden: bool = False,
    plaid_category: dict | None = None,
) -> int:
    with get_db().session() as session:
        plaid = PlaidTransaction(
            external_id=f"p-{ext}",
            source="PLAID",
            account_id="acct-1",
            item_id=None,
            posted_at=date(2026, 1, 10),
            amount_cents=1234,
            currency="USD",
            personal_finance_category=plaid_category,
        )
        session.add(plaid)
        session.flush()
        txn = DerivedTransaction(
            plaid_transaction_id=plaid.plaid_transaction_id,
            external_id=f"d-{ext}",
            amount_cents=1234,
            posted_at=date(2026, 1, 10),
            merchant_descriptor=descriptor,
            category_id=category_id,
            category_method=method if category_id is not None else None,
            category_assigned_at=date(2026, 1, 10) if category_id is not None else None,
            reporting_mode=reporting_mode,
            is_hidden=is_hidden,
        )
        session.add(txn)
        session.flush()
        transaction_id: int = txn.transaction_id
        session.expunge(txn)
    return transaction_id


def test_queue_holds_unreviewed_rows_with_the_agents_pick(
    isolated_db: pytest.FixtureRequest,
) -> None:
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    tid = _txn("1", category_id=groceries, plaid_category={"primary": "FOOD_AND_DRINK"})

    rows = db.review_batch()["rows"]

    assert [r["transaction_id"] for r in rows] == [tid]
    # The page prefills its combobox from this, so a confirm is one keystroke.
    assert rows[0]["agent_key"] == "food.groceries"
    assert rows[0]["plaid_category"]["primary"] == "FOOD_AND_DRINK"
    assert db.pending_review_count() == 1


def test_queue_skips_hidden_rows_and_uncategorizable_investment_trades(
    isolated_db: pytest.FixtureRequest,
) -> None:
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    keep = _txn("keep", category_id=groceries)
    _txn("hidden", category_id=groceries, is_hidden=True)
    # Investment trades carry DEFAULT_EXCLUDE and are never categorized, so
    # there is no decision to review.
    _txn("trade", reporting_mode="DEFAULT_EXCLUDE", category_id=None, method=None)

    assert [r["transaction_id"] for r in db.review_batch()["rows"]] == [keep]
    assert db.pending_review_count() == 1


def test_labeling_removes_the_row_from_the_queue(
    isolated_db: pytest.FixtureRequest,
) -> None:
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    dining = _category("food.restaurants", "Restaurants")
    tid = _txn("1", category_id=groceries)

    result = db.mark_transaction_reviewed(tid, dining)

    assert result == {
        "transaction_id": tid,
        "agent_key": "food.groceries",
        "human_key": "food.restaurants",
        "changed": True,
    }
    assert db.review_batch()["rows"] == []
    assert db.pending_review_count() == 0
    with db.session() as session:
        row = session.get(DerivedTransaction, tid)
        assert row.reviewed_at is not None
        # Verified so the categorizer's fast path reuses the label: reviewing
        # improves the categorizer, not just the measurement of it.
        assert row.is_verified is True


def test_fast_path_verified_rows_are_not_mistaken_for_human_labels(
    isolated_db: pytest.FixtureRequest,
) -> None:
    """``is_verified`` is set by the fast path too, so it cannot mark review."""
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    tid = _txn("1", category_id=groceries, method="manual")
    with db.session() as session:
        session.get(DerivedTransaction, tid).is_verified = True

    # Still queued: verified, but nobody has looked at it.
    assert [r["transaction_id"] for r in db.review_batch()["rows"]] == [tid]
    assert db.review_scoreboard()["reviewed"] == 0


def _sync_day(transaction_id: int, day: date) -> None:
    """Backdate a row's sync timestamp so it lands in an earlier batch."""
    from datetime import datetime

    with get_db().session() as session:
        session.get(DerivedTransaction, transaction_id).created_at = datetime(
            day.year, day.month, day.day, 12, 0
        )


def test_batch_is_one_sync_day_not_the_whole_backlog(
    isolated_db: pytest.FixtureRequest,
) -> None:
    """A sitting is a sync's worth of work, with the rest reachable but not shown.

    Handing over every unreviewed transaction at once is what makes labeling a
    chore nobody starts; the backlog stays one deliberate click away.
    """
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    today = _txn("today", category_id=groceries)
    older = _txn("older", category_id=groceries)
    oldest = _txn("oldest", category_id=groceries)
    _sync_day(older, date(2026, 1, 9))
    _sync_day(oldest, date(2026, 1, 8))

    batch = db.review_batch()

    assert [r["transaction_id"] for r in batch["rows"]] == [today]
    assert batch["total_pending"] == 3
    assert batch["older_pending"] == 2
    # The page offers the next batch, so finishing one leads somewhere.
    assert batch["older_day"] == "2026-01-09"

    # An explicit day opens that batch...
    assert [
        r["transaction_id"] for r in db.review_batch(day=date(2026, 1, 9))["rows"]
    ] == [older]
    # ...and a deliberate catch-up session can still have everything.
    every = db.review_batch(all_days=True)
    assert len(every["rows"]) == 3
    assert every["day"] is None


def test_batch_moves_on_once_a_day_is_labeled(
    isolated_db: pytest.FixtureRequest,
) -> None:
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    today = _txn("today", category_id=groceries)
    older = _txn("older", category_id=groceries)
    _sync_day(older, date(2026, 1, 9))

    db.mark_transaction_reviewed(today, groceries)

    # The newest day with work left becomes the batch.
    batch = db.review_batch()
    assert [r["transaction_id"] for r in batch["rows"]] == [older]
    assert batch["older_day"] is None


def test_scoreboard_scores_against_what_the_model_chose_itself(
    isolated_db: pytest.FixtureRequest,
) -> None:
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    dining = _category("food.restaurants", "Restaurants")

    # An llm event is what the categorizer decided at sync time; the human
    # then overwrites the row's category. The scoreboard must still see the
    # model's own answer, which is why it reads the event log.
    corrected = _txn("wrong", category_id=groceries)
    db.recategorize_transaction(corrected, groceries, verify=False)  # seed history
    with db.session() as session:
        from penny.adapters.db.models import TransactionCategoryEvent

        session.query(TransactionCategoryEvent).filter(
            TransactionCategoryEvent.transaction_id == corrected
        ).update({"method": "llm", "to_category_key": "food.groceries"})
    db.mark_transaction_reviewed(corrected, dining)

    board = db.review_scoreboard()
    item = next(it for it in board["items"] if it["transaction_id"] == corrected)
    assert item["agent_key"] == "food.groceries"
    assert item["human_key"] == "food.restaurants"


def test_scoreboard_separates_rows_the_model_never_decided(
    isolated_db: pytest.FixtureRequest,
) -> None:
    """No llm event → the model made no decision, so there is nothing to score."""
    db = get_db()
    db.create_schema()
    groceries = _category("food.groceries", "Groceries")
    tid = _txn("uncategorized", category_id=None, method=None)

    db.mark_transaction_reviewed(tid, groceries)

    board = db.review_scoreboard()
    assert board["reviewed"] == 1
    assert board["items"][0]["agent_key"] is None
    assert board["items"][0]["human_key"] == "food.groceries"
