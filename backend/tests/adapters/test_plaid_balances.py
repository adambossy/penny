"""`PlaidClient.get_balances` parses /accounts/balance/get responses."""

from typing import Any

from penny.adapters.clients.plaid import PlaidClient


def _client(monkeypatch, response: dict[str, Any]) -> PlaidClient:
    client = PlaidClient(client_id="cid", secret="sec", env="sandbox")
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        return response

    monkeypatch.setattr(client, "_post", fake_post)
    client.calls = calls  # type: ignore[attr-defined]
    return client


def test_get_balances_hits_the_live_balance_endpoint(monkeypatch):
    client = _client(monkeypatch, {"accounts": []})

    client.get_balances("access-sandbox-1")

    path, payload = client.calls[0]  # type: ignore[attr-defined]
    # /accounts/get would serve Plaid's cached copy; balance capture needs the
    # endpoint that forces a live pull from the institution.
    assert path == "/accounts/balance/get"
    assert payload["access_token"] == "access-sandbox-1"


def test_get_balances_parses_a_depository_account(monkeypatch):
    client = _client(
        monkeypatch,
        {
            "accounts": [
                {
                    "account_id": "a1",
                    "name": "Plaid Checking",
                    "official_name": "Plaid Gold Checking",
                    "mask": "0000",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "available": 100.0,
                        "current": 110.0,
                        "limit": None,
                        "iso_currency_code": "USD",
                        "unofficial_currency_code": None,
                    },
                }
            ]
        },
    )

    (account,) = client.get_balances("access-sandbox-1")

    assert account.account_id == "a1"
    assert account.mask == "0000"
    assert account.balances.current == 110.0
    assert account.balances.available == 100.0
    assert account.balances.limit is None
    assert account.balances.iso_currency_code == "USD"


def test_get_balances_tolerates_a_sparse_balances_object(monkeypatch):
    # Plenty of institutions report `current` and nothing else; an account may
    # even arrive with no `balances` key at all. Neither is exceptional.
    client = _client(
        monkeypatch,
        {
            "accounts": [
                {
                    "account_id": "a2",
                    "name": "Brokerage",
                    "type": "investment",
                    "subtype": "brokerage",
                    "balances": {"current": 4321.5},
                },
                {"account_id": "a3", "name": "Mystery"},
            ]
        },
    )

    sparse, bare = client.get_balances("access-sandbox-1")

    assert sparse.balances.current == 4321.5
    assert sparse.balances.available is None
    assert sparse.subtype == "brokerage"
    assert bare.balances.current is None
    assert bare.type is None
