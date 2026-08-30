"""Balance capture: append-only samples, self-healing registration, isolation.

Runs against a real SQLite façade with a fake Plaid client — no network. The
behaviours pinned here are the settled job semantics: one row per account per
run (no dedupe), every returned account registered/refreshed in
``plaid_accounts``, and a broken item reported (relink) or counted (other
failures) without aborting the healthy items.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from penny.adapters.clients.plaid import BalanceAccount, PlaidClientError
from penny.adapters.db.facade import DB
from penny.adapters.db.models import AccountBalance, PlaidAccount, PlaidItem
from penny.tools._services.balance_capture import capture_account_balances

_CAPTURED_AT = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


class FakePlaid:
    """get_balances keyed by access token; an Exception value is raised."""

    def __init__(self, by_token: dict[str, list[BalanceAccount] | Exception]) -> None:
        self._by_token = by_token

    def get_balances(self, access_token: str) -> list[BalanceAccount]:
        result = self._by_token[access_token]
        if isinstance(result, Exception):
            raise result
        return result


def _account(account_id: str, **overrides: object) -> BalanceAccount:
    data: dict[str, object] = {
        "account_id": account_id,
        "name": "Checking",
        "type": "depository",
        "subtype": "checking",
        "balances": {
            "current": 110.55,
            "available": 100.0,
            "limit": None,
            "iso_currency_code": "USD",
        },
    }
    data.update(overrides)
    return BalanceAccount.parse(data)


def _db_with_items(tmp_path: Path, *tokens: str) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_schema()
    with db.session() as session:
        for i, token in enumerate(tokens, start=1):
            session.add(
                PlaidItem(
                    item_id=f"item-{i}",
                    access_token=token,
                    institution_name=f"Bank {i}",
                )
            )
    return db


def _samples(db: DB) -> list[dict[str, object]]:
    """Read samples as plain dicts (the session's expire-on-commit detaches rows)."""
    with db.session() as session:
        rows = session.query(AccountBalance).order_by(AccountBalance.balance_id).all()
        return [
            {
                "account_id": row.account_id,
                "captured_at": row.captured_at,
                "current_cents": row.current_cents,
                "available_cents": row.available_cents,
                "limit_cents": row.limit_cents,
                "currency": row.currency,
            }
            for row in rows
        ]


def test_captures_registers_and_converts_to_cents(tmp_path):
    db = _db_with_items(tmp_path, "tok-1")
    client = FakePlaid({"tok-1": [_account("a1")]})

    summary = capture_account_balances(db, client, captured_at=_CAPTURED_AT)

    # The account was unregistered (the seven-missing-accounts case): the run
    # inserts it with its descriptors rather than needing a repair script.
    assert summary.to_dict() == {
        "accounts_captured": 1,
        "accounts_registered": 1,
        "items_captured": 1,
        "items_failed": 0,
    }
    with db.session() as session:
        registered = session.get(PlaidAccount, "a1")
        assert (registered.type, registered.subtype) == ("depository", "checking")
    (sample,) = _samples(db)
    assert sample["captured_at"] == _CAPTURED_AT.replace(tzinfo=None)
    assert sample["current_cents"] == 11055  # dollars-float → integer cents
    assert sample["available_cents"] == 10000
    assert sample["limit_cents"] is None
    assert sample["currency"] == "USD"


def test_reruns_append_rather_than_dedupe(tmp_path):
    db = _db_with_items(tmp_path, "tok-1")
    client = FakePlaid({"tok-1": [_account("a1")]})

    first = capture_account_balances(db, client, captured_at=_CAPTURED_AT)
    second = capture_account_balances(db, client)

    assert first.accounts_registered == 1
    assert second.accounts_registered == 0  # already registered: refresh only
    assert len(_samples(db)) == 2


def test_broken_item_is_reported_not_fatal(tmp_path):
    db = _db_with_items(tmp_path, "tok-1", "tok-2")
    client = FakePlaid(
        {
            "tok-1": PlaidClientError("Plaid API error 400: ITEM_LOGIN_REQUIRED"),
            "tok-2": [_account("a2")],
        }
    )

    summary = capture_account_balances(db, client, captured_at=_CAPTURED_AT)

    # Bank 1 is a user-action line; Bank 2's capture still landed.
    assert summary.relink_required_items == ["Bank 1"]
    assert summary.items_failed == 0
    assert summary.items_captured == 1
    assert [s["account_id"] for s in _samples(db)] == ["a2"]


def test_undecryptable_token_counts_as_failure(tmp_path):
    db = _db_with_items(tmp_path, "tok-1", "tok-2")
    client = FakePlaid(
        {
            "tok-1": ValueError("no PENNY_PLAID_TOKEN_KEY for token version 2"),
            "tok-2": [_account("a2")],
        }
    )

    summary = capture_account_balances(db, client, captured_at=_CAPTURED_AT)

    assert summary.items_failed == 1
    assert summary.relink_required_items == []
    assert summary.items_captured == 1


def test_unexpected_error_propagates(tmp_path):
    db = _db_with_items(tmp_path, "tok-1")
    client = FakePlaid({"tok-1": RuntimeError("boom")})

    # Only the routine per-item failures are absorbed; a genuine bug surfaces.
    with pytest.raises(RuntimeError):
        capture_account_balances(db, client, captured_at=_CAPTURED_AT)
