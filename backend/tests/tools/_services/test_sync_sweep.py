"""Tests for the end-of-sync categorization sweep + advisory lock.

Covers per-descriptor dedup (one agent run per unique merchant descriptor, with
siblings reusing the decision) and the SQLite no-op advisory lock.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from penny.adapters.db.facade import DB
from penny.adapters.db.models import Category, DerivedTransaction, PlaidTransaction
from penny.taxonomy import loader as taxonomy_loader
from penny.tools._services.sync_service import SyncTool


def _create_db(tmp_path: Path) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'test.db'}", enforce_sqlite_fks=True)
    db.create_schema()
    return db


def _sync_tool(db: DB) -> SyncTool:
    return SyncTool(
        plaid_client=MagicMock(),
        categorizer_factory=MagicMock(),
        db=db,
        taxonomy=MagicMock(),
    )


def _seed_category(db: DB, key: str, name: str) -> int:
    with db.session() as session:
        cat = Category(key=key, name=name)
        session.add(cat)
        session.flush()
        return int(cat.category_id)


def _seed_txn(db: DB, *, external_id: str, descriptor: str) -> int:
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
        txn = DerivedTransaction(
            plaid_transaction_id=plaid.plaid_transaction_id,
            external_id=external_id,
            amount_cents=5000,
            posted_at=date(2026, 1, 10),
            merchant_descriptor=descriptor,
        )
        session.add(txn)
        session.flush()
        return int(txn.transaction_id)


async def test_sweep_dedups_by_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taxonomy_loader._category_id_cache.clear()
    db = _create_db(tmp_path)
    groceries = _seed_category(db, "sweep.groceries", "Groceries")
    # Three rows share a descriptor; one is unique.
    for ext in ("a1", "a2", "a3"):
        _seed_txn(db, external_id=ext, descriptor="ACME")
    _seed_txn(db, external_id="z1", descriptor="ZED")

    calls: list[str] = []

    async def fake_categorize_one(txn: dict) -> dict:
        calls.append(txn["merchant_descriptor"])
        # Mimic the agent persisting the first row of the group.
        db.update_derived_mutable(
            txn["transaction_id"],
            {
                "category_id": groceries,
                "category_method": "llm",
                "category_reason": "agent: groceries",
            },
        )
        return {
            "transaction_id": txn["transaction_id"],
            "category_key": "sweep.groceries",
            "reasoning": "agent: groceries",
        }

    monkeypatch.setattr(
        "penny.tools._services.categorizer_agent.categorize_one", fake_categorize_one
    )
    monkeypatch.setattr("penny.services.get_taxonomy", lambda: MagicMock())

    tool = _sync_tool(db)
    await tool._categorize_uncategorized()

    # One agent run per unique descriptor (not one per row).
    assert sorted(calls) == ["ACME", "ZED"]

    # Every row ended up categorized (siblings reused the decision).
    with db.session() as session:
        cats = [t.category_id for t in session.query(DerivedTransaction).all()]
    assert cats == [groceries] * 4


def test_advisory_lock_is_noop_on_sqlite(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    with db.try_advisory_lock(12345) as acquired:
        assert acquired is True


async def _stub_item_decisions(
    db: DB, monkeypatch: pytest.MonkeyPatch, expected: dict[int, tuple[int, str]]
) -> list[int]:
    """Keep persistence real while making each agent decision deterministic."""
    calls = []

    async def categorize(txn):
        tid = txn["transaction_id"]
        calls.append(tid)
        cid, key = expected[tid]
        db.update_derived_mutable(
            tid,
            {
                "category_id": cid,
                "category_method": "llm",
                "category_reason": f"decision for {key}",
            },
        )
        return {"category_key": key, "reasoning": f"decision for {key}"}

    monkeypatch.setattr(
        "penny.tools._services.categorizer_agent.categorize_one", categorize
    )
    monkeypatch.setattr("penny.services.get_taxonomy", lambda: MagicMock())
    await _sync_tool(db)._categorize_uncategorized()
    return calls


@pytest.mark.parametrize(
    "split_source,same_parent,truncated",
    [
        ("amazon_mutation", True, False),
        ("amazon_mutation", True, True),
        ("amazon_mutation", False, False),
        (None, True, True),
    ],
)
async def test_itemized_rows_never_reuse_another_items_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    split_source: str | None,
    same_parent: bool,
    truncated: bool,
) -> None:
    from penny.adapters.db.models import TransactionCategoryEvent, TransactionItem

    db = _create_db(tmp_path)
    cid_a = _seed_category(db, "test.diapers", "Diapers")
    cid_b = _seed_category(db, "test.books", "Books")
    first = _seed_txn(
        db,
        external_id="item-a",
        descriptor="Amazon: " + ("x" * 50 if truncated else "Diapers"),
    )
    second = _seed_txn(
        db,
        external_id="item-b",
        descriptor="Amazon: " + ("x" * 50 if truncated else "Book"),
    )
    with db.session() as session:
        a = session.get(DerivedTransaction, first)
        b = session.get(DerivedTransaction, second)
        if same_parent:
            b.plaid_transaction_id = a.plaid_transaction_id
        for row in (a, b):
            session.get(
                PlaidTransaction, row.plaid_transaction_id
            ).raw_name = "AMAZON MKTPLACE"
        for row, description in [(a, "Baby diapers"), (b, "Novel")]:
            row.split_source = split_source
            session.add(
                TransactionItem(
                    transaction_id=row.transaction_id,
                    description=description,
                    amount_cents=row.amount_cents,
                    quantity=1,
                    itemization_source="amazon_scrape",
                )
            )
    expected = {first: (cid_a, "test.diapers"), second: (cid_b, "test.books")}
    calls = await _stub_item_decisions(db, monkeypatch, expected)
    assert set(calls) == {first, second}
    with db.session() as session:
        assert session.get(DerivedTransaction, first).category_id == cid_a
        assert session.get(DerivedTransaction, second).category_id == cid_b
        events = session.query(TransactionCategoryEvent).all()
        assert {e.transaction_id: e.categorization_reasoning for e in events} == {
            first: "decision for test.diapers",
            second: "decision for test.books",
        }


async def test_raw_counterparties_stay_distinct_but_ordinary_duplicates_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _create_db(tmp_path)
    cid_a = _seed_category(db, "test.alice", "Alice")
    cid_b = _seed_category(db, "test.bob", "Bob")
    ids = [
        _seed_txn(db, external_id=ext, descriptor="Venmo")
        for ext in ["alice-a", "alice-b", "bob"]
    ]
    with db.session() as session:
        for tid, raw in zip(
            ids, ["VENMO ALICE", "VENMO ALICE", "VENMO BOB"], strict=True
        ):
            row = session.get(DerivedTransaction, tid)
            session.get(PlaidTransaction, row.plaid_transaction_id).raw_name = raw
    expected = {
        ids[0]: (cid_a, "test.alice"),
        ids[1]: (cid_a, "test.alice"),
        ids[2]: (cid_b, "test.bob"),
    }
    calls = await _stub_item_decisions(db, monkeypatch, expected)
    assert set(calls) == {ids[0], ids[2]}
    with db.session() as session:
        assert [session.get(DerivedTransaction, tid).category_id for tid in ids] == [
            cid_a,
            cid_a,
            cid_b,
        ]
