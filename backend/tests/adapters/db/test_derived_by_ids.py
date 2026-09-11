"""Reading derived rows back for the categorizer sweep.

A split leaves several derived rows pointing at one plaid row, so the
eager-loaded parent is shared. The sweep detaches what it returns; detaching
that shared parent once per child used to raise and abort the whole sweep,
leaving every synced transaction uncategorized.
"""

from __future__ import annotations

from datetime import date

import pytest

from penny.adapters.db.models import DerivedTransaction, PlaidTransaction
from penny.db import get_db


def _split_pair() -> list[int]:
    """Two derived rows sharing one plaid transaction, as a split produces."""
    with get_db().session() as session:
        plaid = PlaidTransaction(
            external_id="p-split",
            source="PLAID",
            account_id="acct-1",
            posted_at=date(2026, 1, 10),
            amount_cents=1000,
            currency="USD",
            raw_name="AMAZON MKTPL",
        )
        session.add(plaid)
        session.flush()
        ids = []
        for n, cents in enumerate((400, 600)):
            txn = DerivedTransaction(
                plaid_transaction_id=plaid.plaid_transaction_id,
                external_id=f"d-split-{n}",
                amount_cents=cents,
                posted_at=date(2026, 1, 10),
                merchant_descriptor=f"Amazon item {n}",
            )
            session.add(txn)
            session.flush()
            ids.append(txn.transaction_id)
        return ids


def test_split_rows_come_back_detached_with_their_shared_parent(
    isolated_db: pytest.FixtureRequest,
) -> None:
    db = get_db()
    db.create_schema()
    ids = _split_pair()

    rows = db.get_derived_transactions_by_ids(ids)

    assert sorted(r.transaction_id for r in rows) == sorted(ids)
    # The sweep reads this after the session closed.
    assert [r.plaid_transaction.raw_name for r in rows] == ["AMAZON MKTPL"] * 2
