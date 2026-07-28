"""Plaid access tokens are encrypted at rest and decrypted only at the wire."""

from cryptography.fernet import Fernet
import pytest
import sqlalchemy as sa

from penny.adapters.clients.plaid import PlaidClient
from penny.adapters.db.facade import DB
from penny.security.token_cipher import decrypt_token, encrypt_token, is_encrypted


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("PENNY_PLAID_TOKEN_KEY", k)
    return k


def _db(tmp_path) -> DB:
    db = DB(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_schema()
    return db


def test_save_plaid_item_encrypts_at_rest(tmp_path, key):
    db = _db(tmp_path)
    db.save_plaid_item(item_id="i1", access_token="access-sandbox-123")
    with db.session() as s:
        stored = s.execute(
            sa.text("SELECT access_token FROM plaid_items WHERE item_id='i1'")
        ).scalar()
    assert stored != "access-sandbox-123"
    assert is_encrypted(stored)
    assert decrypt_token(stored) == "access-sandbox-123"


def test_save_plaid_item_never_double_encrypts(tmp_path, key):
    db = _db(tmp_path)
    ct = encrypt_token("access-sandbox-123")
    db.save_plaid_item(item_id="i1", access_token=ct)
    with db.session() as s:
        stored = s.execute(
            sa.text("SELECT access_token FROM plaid_items WHERE item_id='i1'")
        ).scalar()
    assert decrypt_token(stored) == "access-sandbox-123"


def test_save_plaid_item_stores_plaintext_without_key(tmp_path, monkeypatch):
    # Without PENNY_PLAID_TOKEN_KEY the item keeps working in plaintext;
    # encryption engages once the key is configured (migration 017 encrypts the
    # backlog).
    monkeypatch.delenv("PENNY_PLAID_TOKEN_KEY", raising=False)
    db = _db(tmp_path)
    db.save_plaid_item(item_id="i1", access_token="tok")
    with db.session() as s:
        stored = s.execute(sa.text("SELECT access_token FROM plaid_items")).scalar()
    assert stored == "tok"


def test_plaid_client_decrypts_payload_token_at_the_wire(key):
    plaintext = "access-sandbox-123"  # noqa: S105 - test fixture value
    payload = {"access_token": encrypt_token(plaintext), "count": 1}
    prepared = PlaidClient._with_decrypted_token(payload)
    assert prepared["access_token"] == plaintext
    assert prepared["count"] == 1


def test_plaid_client_passes_plaintext_token_through(key):
    payload = {"access_token": "access-sandbox-123"}
    assert PlaidClient._with_decrypted_token(payload) == payload
