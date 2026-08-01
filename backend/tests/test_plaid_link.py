"""Server-side Plaid public-token exchange (hosted Link).

Drives the exchange with a fake Plaid client and stubbed sync — no real Plaid.
Asserts the item is stored encrypted, accounts are persisted, and a
``plaid_link`` reminder is enqueued for the conversation.
"""

from __future__ import annotations

from penny.adapters.db.models import PlaidAccount, PlaidItem
from penny.api.persistence.reminders import DbReminderQueue
from penny.db import get_db
from penny.security.token_cipher import is_encrypted
from penny.tools._services.plaid_link import exchange_public_token
from tests.test_onboarding import _setup


class FakePlaidClient:
    def item_public_token_exchange(self, public_token):
        return {"access_token": "access-sandbox-xyz", "item_id": "item-test-1"}

    def accounts_get(self, access_token):
        return {
            "accounts": [
                {"account_id": "acct-1", "name": "Checking"},
                {"account_id": "acct-2", "name": "Savings"},
            ],
            "item": {"institution_name": "Test Bank"},
        }


async def test_exchange_creates_encrypted_item_accounts_and_reminder(
    isolated_db, monkeypatch
):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PENNY_PLAID_TOKEN_KEY", Fernet.generate_key().decode())
    _setup()
    db = get_db()
    result = await exchange_public_token(
        public_token="public-x",
        conversation_id="conv-1",
        queue=DbReminderQueue(),
        client=FakePlaidClient(),
        sync=lambda item_id: None,
    )
    assert result["accounts"] == 2
    with db.session() as s:
        item = s.query(PlaidItem).filter_by(item_id="item-test-1").one()
        assert is_encrypted(item.access_token)
        accts = s.query(PlaidAccount).filter_by(item_id="item-test-1").all()
        assert {a.account_id for a in accts} == {"acct-1", "acct-2"}
    drained = await DbReminderQueue().drain("conv-1")
    assert drained and drained[0].kind == "plaid_link"
    assert "Test Bank" in drained[0].content
