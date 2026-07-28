from cryptography.fernet import Fernet
import pytest

from penny.security import token_cipher
from penny.security.token_cipher import (
    decrypt_token,
    encrypt_token,
    encrypt_token_at_rest,
    is_encrypted,
)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("PENNY_PLAID_TOKEN_KEY", Fernet.generate_key().decode())


def test_round_trip():
    ct = encrypt_token("access-sandbox-123")
    assert ct != "access-sandbox-123"
    assert is_encrypted(ct)
    assert decrypt_token(ct) == "access-sandbox-123"


def test_ciphertext_carries_key_version_prefix():
    # The v<N>: stamp is what makes later key rotation a config change, not a
    # re-encrypt-everything migration.
    ct = encrypt_token("tok")
    assert ct.startswith("v1:")


def test_unprefixed_legacy_fernet_is_recognized_and_decrypts():
    import os

    key = Fernet(os.environ["PENNY_PLAID_TOKEN_KEY"].encode())
    legacy = key.encrypt(b"tok").decode()
    assert is_encrypted(legacy)  # gAAAAA sniff
    assert decrypt_token(legacy) == "tok"


def test_plaintext_is_not_encrypted():
    assert not is_encrypted("access-sandbox-123")


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("PENNY_PLAID_TOKEN_KEY", raising=False)
    with pytest.raises(RuntimeError):
        encrypt_token("tok")


def test_unknown_key_version_raises():
    with pytest.raises(ValueError):
        decrypt_token("v9:whatever")


def test_prior_version_key_still_decrypts_after_rotation(monkeypatch):
    # F03 regression: bumping the active version must NOT orphan existing
    # ciphertext. Encrypt at v1, then rotate to v2 keeping the v1 key configured.
    k1 = Fernet.generate_key().decode()
    monkeypatch.setenv("PENNY_PLAID_TOKEN_KEY", k1)  # v1 key
    ct_v1 = encrypt_token("access-1")
    assert ct_v1.startswith("v1:")

    # Rotation: introduce a v2 key and make v2 the active version. The v1 key
    # stays set (retained for decrypt of the backlog).
    k2 = Fernet.generate_key().decode()
    assert k2 != k1
    monkeypatch.setenv("PENNY_PLAID_TOKEN_KEY_V2", k2)
    monkeypatch.setattr(token_cipher, "_ACTIVE_VERSION", 2)

    # The pre-rotation ciphertext still decrypts with the retained v1 key.
    assert decrypt_token(ct_v1) == "access-1"

    # New writes use the v2 key and stamp.
    ct_v2 = encrypt_token("access-2")
    assert ct_v2.startswith("v2:")
    assert decrypt_token(ct_v2) == "access-2"
    # And the v2 ciphertext is genuinely under k2, not k1.
    assert Fernet(k2.encode()).decrypt(ct_v2.split(":", 1)[1].encode()) == b"access-2"


def test_encrypt_token_at_rest_encrypts_when_key_present():
    # The autouse _key fixture provides PENNY_PLAID_TOKEN_KEY.
    ct = encrypt_token_at_rest("tok")
    assert is_encrypted(ct)
    assert decrypt_token(ct) == "tok"


def test_encrypt_token_at_rest_allows_plaintext_without_key(monkeypatch):
    monkeypatch.delenv("PENNY_PLAID_TOKEN_KEY", raising=False)
    assert encrypt_token_at_rest("tok") == "tok"
