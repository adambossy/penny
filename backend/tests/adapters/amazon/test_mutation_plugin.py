"""Tests for AmazonMutationPlugin: items writes and idempotency."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from penny.adapters.amazon.mutation_plugin import (
    AmazonMutationPlugin,
    AmazonMutationPluginConfig,
)
from penny.adapters.db.facade import DB
from penny.adapters.db.models import (
    DerivedTransaction,
    PlaidTransaction,
    TransactionItem,
)


def _create_db(tmp_path: Path) -> DB:
    """Create a file-backed SQLite DB with full schema.

    Enforces SQLite FK constraints so ON DELETE CASCADE behaves like PostgreSQL
    — the idempotency test (test_amazon_mutation_resync_idempotent_via_delete_facade)
    relies on cascade deletes firing when delete_derived_by_plaid_ids runs.
    """
    db = DB(f"sqlite:///{tmp_path / 'test.db'}", enforce_sqlite_fks=True)
    db.create_schema()
    return db


def _insert_plaid_txn(
    db: DB,
    *,
    external_id: str = "plaid-amz-001",
    amount_cents: int = 6000,
    posted_at: date = date(2026, 2, 10),
    merchant_descriptor: str = "Amazon",
) -> int:
    """Insert a minimal Amazon-like PlaidTransaction; return its PK."""
    with db.session() as session:
        plaid_txn = PlaidTransaction(
            external_id=external_id,
            source="PLAID",
            account_id="acct-abc",
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


def _seed_amazon_order(
    db: DB,
    plaid_txn_amount_cents: int,
) -> str:
    """Seed an Amazon order with 3 items; return order_id."""
    order_id = "113-1111111-1111111"
    profile = db.create_amazon_login_profile(
        profile_key="primary", display_name="Primary"
    )
    db.upsert_amazon_order(
        order_id=order_id,
        order_date=date(2026, 2, 8),
        order_total_cents=plaid_txn_amount_cents,
        tax_cents=0,
        shipping_cents=0,
        profile_id=profile.profile_id,
    )
    db.upsert_amazon_item(
        order_id=order_id,
        asin="B001",
        description="Wireless Mouse",
        price_cents=2000,
        quantity=1,
    )
    db.upsert_amazon_item(
        order_id=order_id,
        asin="B002",
        description="USB Hub",
        price_cents=2500,
        quantity=1,
    )
    db.upsert_amazon_item(
        order_id=order_id,
        asin="B003",
        description="HDMI Cable",
        price_cents=1500,
        quantity=1,
    )
    return order_id


def _run_mutation(db: DB, plaid_id: int) -> list[int]:
    """Initialize and run the Amazon mutation plugin; return new derived IDs."""
    plugin = AmazonMutationPlugin(db, AmazonMutationPluginConfig())
    plaid_txns = db.get_plaid_transactions_by_ids([plaid_id])
    plaid_txn = plaid_txns[plaid_id]

    plugin.initialize(list(plaid_txns.values()))

    old_derived = db.get_derived_by_plaid_ids([plaid_id]).get(plaid_id, [])
    result = plugin.process(plaid_txn, old_derived)
    return db.bulk_insert_derived_transactions(result.derived_data_list)


def _count_items(db: DB, transaction_id: int) -> int:
    """Count TransactionItem rows for a given transaction_id."""
    with db.session() as session:
        return (
            session.query(TransactionItem)
            .filter_by(transaction_id=transaction_id)
            .count()
        )


def _fetch_items(db: DB, transaction_id: int) -> list[TransactionItem]:
    """Fetch all TransactionItem rows for a given transaction_id."""
    with db.session() as session:
        items = (
            session.query(TransactionItem)
            .filter_by(transaction_id=transaction_id)
            .all()
        )
        for item in items:
            session.expunge(item)
        return items


def _fetch_derived_rows(db: DB, plaid_id: int) -> list[DerivedTransaction]:
    """Fetch all DerivedTransaction rows for a given plaid_transaction_id."""
    with db.session() as session:
        rows = (
            session.query(DerivedTransaction)
            .filter_by(plaid_transaction_id=plaid_id)
            .order_by(DerivedTransaction.split_index)
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _as_item_dict(item: TransactionItem) -> dict[str, object]:
    """Extract the fields we care about from a TransactionItem."""
    return {
        "itemization_source": item.itemization_source,
        "source_ref": item.source_ref,
    }


def test_amazon_mutation_writes_transaction_items(tmp_path: Path) -> None:
    """AmazonMutationPlugin writes one TransactionItem per amazon_item."""
    # input
    db = _create_db(tmp_path)
    total_cents = 6000
    plaid_id = _insert_plaid_txn(db, amount_cents=total_cents)
    order_id = _seed_amazon_order(db, total_cents)

    # act
    derived_ids = _run_mutation(db, plaid_id)
    all_items = [item for did in derived_ids for item in _fetch_items(db, did)]

    # expected
    expected_output = {
        "derived_count": 3,
        "items_per_derived": [1, 1, 1],
        "total_item_cents": total_cents,
        "item_sources": [
            {"itemization_source": "amazon_scrape", "source_ref": order_id}
        ]
        * 3,
    }

    # assert
    assert {
        "derived_count": len(derived_ids),
        "items_per_derived": [_count_items(db, did) for did in derived_ids],
        "total_item_cents": sum(item.amount_cents for item in all_items),
        "item_sources": [_as_item_dict(item) for item in all_items],
    } == expected_output


def test_amazon_mutation_resync_idempotent(tmp_path: Path) -> None:
    """Re-running mutation produces no duplicate transaction_items rows.

    Simulates re-sync by deleting old derived rows via ORM (which respects
    the SQLAlchemy cascade="all, delete-orphan" relationship), then re-running
    the mutation.  In production PostgreSQL, the equivalent guarantee is
    provided by the ON DELETE CASCADE constraint on transaction_items.
    """
    # input
    db = _create_db(tmp_path)
    total_cents = 6000
    plaid_id = _insert_plaid_txn(db, amount_cents=total_cents)
    _seed_amazon_order(db, total_cents)

    # act: run mutation once
    first_ids = _run_mutation(db, plaid_id)
    assert len(first_ids) == 3
    # Verify items from first run
    first_item_total = sum(_count_items(db, tid) for tid in first_ids)
    assert first_item_total == 3

    # Delete old derived rows via ORM (respects cascade, matching production behavior)
    with db.session() as session:
        rows = (
            session.query(DerivedTransaction)
            .filter(DerivedTransaction.plaid_transaction_id == plaid_id)
            .all()
        )
        for row in rows:
            session.delete(row)

    # act: run mutation a second time
    second_ids = _run_mutation(db, plaid_id)

    # expected: still 3 derived rows, still 1 item each — no duplicates
    expected_derived_count = 3

    # assert
    assert len(second_ids) == expected_derived_count
    total_item_count = sum(_count_items(db, tid) for tid in second_ids)
    assert total_item_count == expected_derived_count


def test_amazon_mutation_split_group_id_and_index(tmp_path: Path) -> None:
    """Derived rows from one Amazon split share split_group_id; indexes are 0..N-1."""
    # input
    db = _create_db(tmp_path)
    total_cents = 6000
    plaid_id = _insert_plaid_txn(db, amount_cents=total_cents)
    _seed_amazon_order(db, total_cents)

    # act
    derived_ids = _run_mutation(db, plaid_id)
    derived_rows = _fetch_derived_rows(db, plaid_id)

    # expected: 3 rows, all with the same non-None split_group_id, split_index 0..2
    expected_output = {
        "count": 3,
        "split_group_ids_unique": 1,
        "split_group_id_is_set": True,
        "split_indexes": [0, 1, 2],
    }

    group_ids = {row.split_group_id for row in derived_rows}
    all_set = all(row.split_group_id is not None for row in derived_rows)
    indexes = sorted(
        row.split_index for row in derived_rows if row.split_index is not None
    )

    # assert
    assert {
        "count": len(derived_ids),
        "split_group_ids_unique": len(group_ids),
        "split_group_id_is_set": all_set,
        "split_indexes": indexes,
    } == expected_output


def test_amazon_mutation_resync_idempotent_via_delete_facade(tmp_path: Path) -> None:
    """Re-running mutation via delete_derived_by_plaid_ids produces no duplicate items.

    This exercises the production delete path (bulk SQL DELETE with ON DELETE CASCADE)
    rather than the ORM cascade path tested by test_amazon_mutation_resync_idempotent.
    """
    # input
    db = _create_db(tmp_path)
    total_cents = 6000
    plaid_id = _insert_plaid_txn(db, amount_cents=total_cents)
    _seed_amazon_order(db, total_cents)

    # act: run mutation once
    first_ids = _run_mutation(db, plaid_id)
    assert len(first_ids) == 3
    first_item_total = sum(_count_items(db, tid) for tid in first_ids)
    assert first_item_total == 3

    # Delete via the public facade method (production code path)
    deleted = db.delete_derived_by_plaid_ids([plaid_id])
    assert deleted == 3

    # act: run mutation a second time
    second_ids = _run_mutation(db, plaid_id)

    # expected: 3 derived rows, 1 item each — no duplicates
    expected_output = {
        "derived_count": 3,
        "total_item_count": 3,
    }

    # assert
    assert {
        "derived_count": len(second_ids),
        "total_item_count": sum(_count_items(db, tid) for tid in second_ids),
    } == expected_output


def _sync_tool(db: DB):
    from unittest.mock import AsyncMock, MagicMock

    from penny.tools._services.sync_service import SyncTool

    tool = SyncTool(
        plaid_client=MagicMock(),
        categorizer_factory=MagicMock(),
        db=db,
        taxonomy=MagicMock(),
    )
    tool._categorize_derived = AsyncMock()
    return tool


def _snapshot(db: DB, plaid_id: int) -> list[tuple]:
    return sorted(
        (
            row.transaction_id,
            row.category_id,
            row.is_verified,
            row.category_method,
            row.split_group_id,
            row.is_hidden,
            tuple(
                sorted(
                    (i.item_id, i.source_ref, i.description, i.quantity, i.amount_cents)
                    for i in row.items
                )
            ),
        )
        for row in db.get_derived_by_plaid_ids([plaid_id])[plaid_id]
    )


@pytest.mark.parametrize("sign", [1, -1])
def test_remutation_identical_scrape_preserves_verified_rows(
    tmp_path: Path, sign: int
) -> None:
    from penny.adapters.db.models import Category
    from penny.plugins.amazon.remutate import remutate_amazon_orders

    db = _create_db(tmp_path)
    db.set_sign_convention(
        "acct-abc", "expense_positive" if sign == 1 else "expense_negative"
    )
    plaid_id = _insert_plaid_txn(db, amount_cents=6000 * sign)
    order_id = _seed_amazon_order(db, 6000)
    tool = _sync_tool(db)
    ids = tool._mutate_batch_to_derived([plaid_id])
    with db.session() as session:
        category = Category(key="test.manual", name="Manual")
        session.add(category)
        session.flush()
        row = session.get(DerivedTransaction, ids[0])
        row.category_id = category.category_id
        row.category_method = "manual"
        row.category_assigned_at = datetime(2026, 2, 11)
        row.is_verified = True
        row.is_hidden = True
    before = _snapshot(db, plaid_id)
    # Repeat the scrape in a different order; timestamps change but content does not.
    for asin, description, price in [
        ("B003", "HDMI Cable", 1500),
        ("B002", "USB Hub", 2500),
        ("B001", "Wireless Mouse", 2000),
    ]:
        db.upsert_amazon_item(order_id, asin, description, price)
    for dry_run in (True, False):
        result = remutate_amazon_orders(
            db,
            dry_run=dry_run,
            sync_tool_factory=lambda _: tool,
        )
        assert result["unchanged"] == 1
        assert result["changed"] == 0
        assert result["mutation_conflicts"] == []
        assert result["overwrites"] == 0
        assert _snapshot(db, plaid_id) == before
    tool._categorize_derived.assert_not_called()


def test_remutation_conflict_does_not_block_other_changes(tmp_path: Path) -> None:
    from penny.plugins.amazon.remutate import remutate_amazon_orders

    db = _create_db(tmp_path)
    protected_id = _insert_plaid_txn(db)
    order_id = _seed_amazon_order(db, 6000)
    tool = _sync_tool(db)
    ids = tool._mutate_batch_to_derived([protected_id])
    with db.session() as session:
        session.get(DerivedTransaction, ids[0]).is_verified = True
    before = _snapshot(db, protected_id)
    # Changed item data must be reported without replacing the verified group.
    db.upsert_amazon_item(order_id, "B001", "Wireless Mouse revised", 2000)
    other_id = _insert_plaid_txn(db, external_id="other", amount_cents=3000)
    profile = db.list_amazon_orders()[0].profile_id
    db.upsert_amazon_order("other-order", date(2026, 2, 8), 3000, profile_id=profile)
    db.upsert_amazon_item("other-order", "B004", "Book", 3000)
    preview = remutate_amazon_orders(db, dry_run=True)
    assert preview["changed"] == 1
    assert len(preview["mutation_conflicts"]) == 1
    assert db.get_derived_by_plaid_ids([other_id])[other_id] == []
    result = remutate_amazon_orders(db, sync_tool_factory=lambda _: tool)
    assert result["mutation_conflicts"] == preview["mutation_conflicts"]
    assert result["changed"] == 1
    assert result["derived_after_split"] == 1
    assert result["overwrites"] == 0
    conflict = result["mutation_conflicts"][0]
    assert conflict["plaid_transaction_id"] == protected_id
    assert conflict["verified_transaction_ids"] == [ids[0]]
    assert (
        conflict["proposed"][0]["items"][0]["description"] == "Wireless Mouse revised"
    )
    assert _snapshot(db, protected_id) == before
    new_rows = db.get_derived_by_plaid_ids([other_id])[other_id]
    assert len(new_rows) == 1
    tool._categorize_derived.assert_awaited_once_with([new_rows[0].transaction_id])


def test_unverified_splits_noop_then_replace_changed_item_data(tmp_path: Path) -> None:
    from penny.tools._services.mutation_reconciliation import MutationReport

    db = _create_db(tmp_path)
    plaid_id = _insert_plaid_txn(db)
    order_id = _seed_amazon_order(db, 6000)
    tool = _sync_tool(db)
    tool._mutate_batch_to_derived([plaid_id])
    before = _snapshot(db, plaid_id)
    report = MutationReport()
    tool._mutate_batch_to_derived([plaid_id], report=report)
    assert report.unchanged == [plaid_id]
    assert _snapshot(db, plaid_id) == before
    db.upsert_amazon_item(order_id, "B001", "Wireless Mouse", 1000, quantity=2)
    report = MutationReport()
    tool._mutate_batch_to_derived([plaid_id], report=report)
    assert report.changed == [plaid_id]
    assert report.conflicts == []
    rows = db.get_derived_by_plaid_ids([plaid_id])[plaid_id]
    assert sum(row.amount_cents for row in rows) == 6000
    assert sorted(i.quantity for row in rows for i in row.items) == [1, 1, 2]


async def test_sync_returns_conflicts_across_pages_and_finishes_other_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    from penny.adapters.db.models import PlaidItem
    from penny.tools._services.sync_service import SyncTool
    from penny.tools.sync import sync_transactions

    db = _create_db(tmp_path)
    plaid_id = _insert_plaid_txn(db)
    order_id = _seed_amazon_order(db, 6000)
    tool = _sync_tool(db)
    ids = tool._mutate_batch_to_derived([plaid_id])
    with db.session() as session:
        session.get(DerivedTransaction, ids[0]).is_verified = True
        session.add(PlaidItem(item_id="item", access_token="test-token"))
    before = _snapshot(db, plaid_id)
    second_id = _insert_plaid_txn(
        db,
        external_id="second-protected",
        amount_cents=1000,
        merchant_descriptor="Grocer",
    )
    second_rows = tool._mutate_batch_to_derived([second_id])
    with db.session() as session:
        session.get(DerivedTransaction, second_rows[0]).is_verified = True
    second_before = _snapshot(db, second_id)
    db.upsert_amazon_item(order_id, "B001", "Changed item", 2000)
    tool._plaid_client.get_accounts.return_value = []
    tool._plaid_client.sync_transactions.side_effect = [
        {
            "modified": [
                {
                    "transaction_id": "plaid-amz-001",
                    "account_id": "acct-abc",
                    "date": "2026-02-10",
                    "amount": 60,
                    "name": "Amazon",
                }
            ],
            "has_more": True,
            "next_cursor": "page-2",
        },
        {
            "added": [
                {
                    "transaction_id": "unrelated",
                    "account_id": "acct-abc",
                    "date": "2026-02-11",
                    "amount": 7,
                    "name": "Grocer",
                }
            ],
            "modified": [
                {
                    "transaction_id": "second-protected",
                    "account_id": "acct-abc",
                    "date": "2026-02-10",
                    "amount": 12,
                    "name": "Grocer",
                }
            ],
            "has_more": False,
            "next_cursor": "done",
        },
    ]
    tool._resolve_merchant_ids = AsyncMock(return_value={})
    tool._sync_investments_for_item = AsyncMock(return_value=(0, 0, 0, None))
    tool._categorize_uncategorized = AsyncMock()
    monkeypatch.setattr(SyncTool, "from_env", lambda: tool)
    result = await sync_transactions.fn()
    assert result["status"] == "success"
    assert result["total_added"] == 1
    assert result["total_modified"] == 2
    assert {c["plaid_transaction_id"] for c in result["mutation_conflicts"]} == {
        plaid_id,
        second_id,
    }
    assert _snapshot(db, second_id) == second_before
    assert _snapshot(db, plaid_id) == before
    assert db.get_sync_cursor("item") == "done"
    with db.session() as session:
        assert (
            session.query(DerivedTransaction).filter_by(external_id="unrelated").count()
            == 1
        )
    tool._categorize_uncategorized.assert_awaited_once()


@pytest.mark.parametrize("verified", [False, True])
def test_late_item_detail_is_change_even_when_amount_and_descriptor_match(
    tmp_path: Path,
    verified: bool,
) -> None:
    from penny.plugins.amazon.remutate import remutate_amazon_orders

    db = _create_db(tmp_path)
    plaid_id = _insert_plaid_txn(db, merchant_descriptor="Amazon: Book")
    tool = _sync_tool(db)
    ids = tool._mutate_batch_to_derived([plaid_id])
    with db.session() as session:
        session.get(DerivedTransaction, ids[0]).is_verified = verified
    profile = db.create_amazon_login_profile(
        profile_key="primary", display_name="Primary"
    )
    db.upsert_amazon_order(
        "late", date(2026, 2, 8), 6000, profile_id=profile.profile_id
    )
    db.upsert_amazon_item("late", "B001", "Book", 6000)
    preview = remutate_amazon_orders(db, dry_run=True)
    result = remutate_amazon_orders(db, sync_tool_factory=lambda _: tool)
    assert result["mutation_conflicts"] == preview["mutation_conflicts"]
    assert result["unchanged"] == 0
    assert result["changed"] == (0 if verified else 1)
    rows = db.get_derived_by_plaid_ids([plaid_id])[plaid_id]
    assert len(rows[0].items) == (0 if verified else 1)
    assert len(result["mutation_conflicts"]) == (1 if verified else 0)


def test_legacy_rounding_order_is_preserved_but_price_changes_are_detected(
    tmp_path: Path,
) -> None:
    from penny.adapters.amazon.entities import AmazonItem, AmazonOrder
    from penny.adapters.amazon.splitter import split_order_to_derived
    from penny.tools._services.mutation_plugin import (
        DerivedTransactionPayload,
        TransactionItemPayload,
    )
    from penny.tools._services.mutation_reconciliation import MutationReport

    db = _create_db(tmp_path)
    pid = _insert_plaid_txn(db, amount_cents=201)
    profile = db.create_amazon_login_profile(
        profile_key="primary", display_name="Primary"
    )
    db.upsert_amazon_order(
        "legacy", date(2026, 2, 8), 201, profile_id=profile.profile_id
    )
    items = [
        AmazonItem("legacy", "Mouse", 100, 1, "B002"),
        AmazonItem("legacy", "Book", 100, 1, "B001"),
    ]
    for item in items:
        db.upsert_amazon_item(
            item.order_id, item.asin, item.description, item.price_cents
        )
    plaid = db.get_plaid_transactions_by_ids([pid])[pid]
    old = split_order_to_derived(
        plaid, AmazonOrder("legacy", date(2026, 2, 8), 201, 0, 0), items
    )
    db.bulk_insert_derived_transactions(
        [
            DerivedTransactionPayload(
                plaid_transaction_id=pid,
                external_id=row.external_id,
                amount_cents=row.amount_cents,
                posted_at=row.posted_at,
                merchant_descriptor=row.merchant_descriptor,
                is_verified=True,
                split_source="amazon_mutation",
                split_group_id="legacy",
                split_index=idx,
                items=[
                    TransactionItemPayload(
                        description=items[idx].description,
                        amount_cents=row.amount_cents,
                        source_ref="legacy",
                    )
                ],
            )
            for idx, row in enumerate(old)
        ]
    )
    before = _snapshot(db, pid)
    tool = _sync_tool(db)
    report = MutationReport()
    tool._mutate_batch_to_derived([pid], report=report)
    assert report.unchanged == [pid]
    assert report.conflicts == []
    assert _snapshot(db, pid) == before
    db.upsert_amazon_item("legacy", "B002", "Mouse", 120)
    report = MutationReport()
    tool._mutate_batch_to_derived([pid], report=report)
    assert len(report.conflicts) == 1
    assert _snapshot(db, pid) == before


def test_matching_ties_do_not_depend_on_batch_order(tmp_path: Path) -> None:
    db = _create_db(tmp_path)
    first = _insert_plaid_txn(db)
    second = _insert_plaid_txn(db, external_id="same-date-amount")
    _seed_amazon_order(db, 6000)
    txns = db.get_plaid_transactions_by_ids([first, second])
    plugin = AmazonMutationPlugin(db, AmazonMutationPluginConfig())
    plugin.initialize([txns[second], txns[first]])
    assert plugin.should_handle(txns[first])
    assert not plugin.should_handle(txns[second])
    plugin.initialize([txns[first], txns[second]])
    assert plugin.should_handle(txns[first])
    assert not plugin.should_handle(txns[second])
