"""Façade seams for the daily balance capture: registration + append-only rows.

``register_plaid_accounts`` is the self-healing registrar (insert missing
accounts, refresh descriptors on the rest); ``add_account_balances`` appends
one sample per account per run and never dedupes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.dialects import postgresql

from penny.adapters.db.facade import DB
from penny.adapters.db.models import AccountBalance, PlaidAccount, PlaidItem


def _create_db(tmp_path: Path) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_schema()
    with db.session() as session:
        session.add(PlaidItem(item_id="item-1", access_token="tok"))
    return db


def test_register_inserts_missing_and_refreshes_existing(tmp_path):
    db = _create_db(tmp_path)
    with db.session() as session:
        # An account registered by an old backfill: no type/subtype yet.
        session.add(PlaidAccount(account_id="a1", item_id="item-1", name="Old name"))

    inserted = db.register_plaid_accounts(
        "item-1",
        [
            {
                "account_id": "a1",
                "name": "Checking",
                "type": "depository",
                "subtype": "checking",
            },
            {
                "account_id": "a2",
                "name": "Card",
                "type": "credit",
                "subtype": "credit card",
            },
        ],
    )

    # a2 was missing (the self-healing case); a1 only got refreshed.
    assert inserted == 1
    with db.session() as session:
        a1 = session.get(PlaidAccount, "a1")
        a2 = session.get(PlaidAccount, "a2")
        assert (a1.name, a1.type, a1.subtype) == ("Checking", "depository", "checking")
        assert (a2.item_id, a2.subtype) == ("item-1", "credit card")


def test_register_is_idempotent(tmp_path):
    db = _create_db(tmp_path)
    accounts = [
        {
            "account_id": "a1",
            "name": "Checking",
            "type": "depository",
            "subtype": "checking",
        }
    ]

    assert db.register_plaid_accounts("item-1", accounts) == 1
    assert db.register_plaid_accounts("item-1", accounts) == 0


def test_add_account_balances_appends_without_dedupe(tmp_path):
    db = _create_db(tmp_path)
    db.register_plaid_accounts("item-1", [{"account_id": "a1", "name": "Checking"}])
    captured_at = datetime(2026, 8, 30, 11, 0, tzinfo=UTC)
    row = {
        "account_id": "a1",
        "captured_at": captured_at,
        "current_cents": 12345,
        "available_cents": None,
        "limit_cents": None,
        "currency": "USD",
    }

    # Two identical runs → two samples: append-only, no upsert.
    assert db.add_account_balances([row]) == 1
    assert db.add_account_balances([dict(row)]) == 1

    with db.session() as session:
        samples = (
            session.query(AccountBalance).order_by(AccountBalance.balance_id).all()
        )
        assert len(samples) == 2
        assert samples[0].current_cents == 12345
        assert samples[0].available_cents is None
        assert samples[1].balance_id > samples[0].balance_id


def test_money_columns_are_bigint_on_postgres():
    """Money columns must be int8 on Postgres: int4 caps an account at ~$21.5M.

    The drift test compares column names only, and SQLite is dynamically int64
    either way, so a narrowing here would only surface as a production
    overflow — pin the dialect-compiled type instead.
    """
    dialect = postgresql.dialect()
    for name in ("balance_id", "current_cents", "available_cents", "limit_cents"):
        column_type = AccountBalance.__table__.c[name].type
        assert column_type.compile(dialect) == "BIGINT", name


def test_schema_hint_documents_the_new_tables(tmp_path):
    hint = _create_db(tmp_path).compact_schema_hint()["tables"]

    assert "captured_at" in hint["account_balances"]["columns"]
    notes = hint["account_balances"]["notes"]
    assert "Append-only" in notes
    assert "UTC" in notes
    assert "OWED" in notes
    assert "subtype" in hint["plaid_accounts"]["columns"]
