"""The review page, its label endpoint, and the scoreboard.

Also guards the routing order that makes the page reachable at all: the SPA's
static mount owns ``/``, so ``/review`` only works while its router is
included ahead of that mount.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
import pytest

from penny.adapters.db.models import (
    Category,
    DerivedTransaction,
    PlaidTransaction,
    TransactionCategoryEvent,
)
from penny.api.app import create_app
from penny.db import get_db


def _seed() -> tuple[int, str]:
    """One transaction as sync leaves it: categorized, with its ``llm`` event.

    The event is what the scoreboard reads back as the model's own answer, so
    seeding without it would not exercise scoring at all.
    """
    db = get_db()
    db.create_schema()
    with db.session() as session:
        for key, name in (
            ("food.groceries", "Groceries"),
            ("food.restaurants", "Restaurants"),
        ):
            session.add(Category(key=key, name=name))
        session.flush()
        groceries = (
            session.query(Category).filter(Category.key == "food.groceries").one()
        )
        plaid = PlaidTransaction(
            external_id="p-1",
            source="PLAID",
            account_id="acct-1",
            item_id=None,
            posted_at=date(2026, 1, 10),
            amount_cents=1234,
            currency="USD",
        )
        session.add(plaid)
        session.flush()
        txn = DerivedTransaction(
            plaid_transaction_id=plaid.plaid_transaction_id,
            external_id="d-1",
            amount_cents=1234,
            posted_at=date(2026, 1, 10),
            merchant_descriptor="Jubilee Market",
            category_id=groceries.category_id,
            category_method="llm",
            category_assigned_at=date(2026, 1, 10),
        )
        session.add(txn)
        session.flush()
        session.add(
            TransactionCategoryEvent(
                transaction_id=txn.transaction_id,
                from_category_id=None,
                to_category_id=groceries.category_id,
                from_category_key=None,
                to_category_key="food.groceries",
                method="llm",
            )
        )
        session.flush()
        return txn.transaction_id, "food.groceries"


def test_review_page_renders_the_queue(isolated_db: pytest.FixtureRequest) -> None:
    tid, agent_key = _seed()
    with TestClient(create_app()) as client:
        res = client.get("/review")
    assert res.status_code == 200
    assert "Jubilee Market" in res.text
    # The row and the taxonomy both ride in the page — it needs no further
    # fetches to be usable.
    assert str(tid) in res.text
    assert agent_key in res.text


def test_label_records_the_human_choice(isolated_db: pytest.FixtureRequest) -> None:
    tid, _ = _seed()
    with TestClient(create_app()) as client:
        res = client.post(
            "/api/review/label",
            json={"transaction_id": tid, "category_key": "food.restaurants"},
        )
        assert res.status_code == 200
        assert res.json()["human_key"] == "food.restaurants"
        # Labeled rows leave the queue.
        assert "Jubilee Market" not in client.get("/review").text
        assert "food.restaurants" in client.get("/review/metrics").text


def test_label_rejects_an_unknown_category(isolated_db: pytest.FixtureRequest) -> None:
    tid, _ = _seed()
    with TestClient(create_app()) as client:
        res = client.post(
            "/api/review/label",
            json={"transaction_id": tid, "category_key": "not.a.category"},
        )
    assert res.status_code == 400
    assert get_db().pending_review_count() == 1


def test_label_404s_on_a_missing_transaction(
    isolated_db: pytest.FixtureRequest,
) -> None:
    _seed()
    with TestClient(create_app()) as client:
        res = client.post(
            "/api/review/label",
            json={"transaction_id": 99999, "category_key": "food.groceries"},
        )
    assert res.status_code == 404
