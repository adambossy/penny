"""Fernet encryption for secrets at rest (Plaid access tokens, BYO credentials).

Stored ciphertext is stamped with a key version — ``v1:<fernet-token>`` — so key
rotation is a config change, not a re-encrypt-everything migration:

- ``encrypt_token`` writes with the *active* version's key.
- ``decrypt_token`` selects the key by the ciphertext's own version prefix, so
  values written under *any* still-configured version keep decrypting. Bare
  ``gAAAAA…`` Fernet values from before the stamp decrypt with the v1 key.

Each version's key lives in its own env var: v1 in ``PENNY_PLAID_TOKEN_KEY`` (the
original single-key var; ``PENNY_PLAID_TOKEN_KEY_V1`` is also accepted), and
v>=2 in ``PENNY_PLAID_TOKEN_KEY_V<N>``. Rotation: add ``PENNY_PLAID_TOKEN_KEY_V2``,
bump ``_ACTIVE_VERSION`` to 2, and keep ``PENNY_PLAID_TOKEN_KEY`` set so existing
``v1:`` ciphertext still decrypts. Removing a version's key only after all its
ciphertext has been re-encrypted forward is the (optional) final rotation step.

The plaintext is decrypted only at the outbound call site (the Plaid API, or the
outbound-LLM provider client for a BYO credential); it must never appear in logs,
prompts, or the browser.

``encrypt_token``/``decrypt_token`` are the original Plaid-token surface;
``encrypt_secret``/``decrypt_secret`` (phase 2b) are the generic aliases the
billing credential vault uses. Both share the same versioned envelope and key set.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

# The active key version written by encrypt_token. Rotation bumps this after the
# new version's key env var is in place; prior versions stay decryptable.
_ACTIVE_VERSION = 1

# Fernet tokens are base64 starting with 0x80 0x00.. — always "gAAAAA".
_FERNET_SNIFF = "gAAAAA"


def _key_env_for_version(version: int) -> str | None:
    """The configured Fernet key string for ``version``, or None if unset.

    v1 reads ``PENNY_PLAID_TOKEN_KEY`` first (the original single-key var), then
    ``PENNY_PLAID_TOKEN_KEY_V1``; v>=2 reads ``PENNY_PLAID_TOKEN_KEY_V<version>``.
    """
    names = [f"PENNY_PLAID_TOKEN_KEY_V{version}"]
    if version == 1:
        names.insert(0, "PENNY_PLAID_TOKEN_KEY")
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _fernet_for_version(version: int) -> Fernet:
    """Fernet for a specific key version; raises if that version's key is unset.

    ``ValueError`` (not ``RuntimeError``) because a missing *decrypt* key means a
    ciphertext references a version this deployment cannot read — a data/rotation
    problem, distinct from the active-key-missing config error at encrypt time.
    """
    key = _key_env_for_version(version)
    if key is None:
        raise ValueError(f"No Plaid token key configured for version v{version}")
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> str:
    """Encrypt with the active version's key, stamped ``v<N>:``."""
    if _key_env_for_version(_ACTIVE_VERSION) is None:
        # Fail closed rather than silently store plaintext (the storage sites add
        # the clerk-mode guard). Historical message/type preserved for callers.
        raise RuntimeError("PENNY_PLAID_TOKEN_KEY is not set")
    ciphertext = (
        _fernet_for_version(_ACTIVE_VERSION).encrypt(plaintext.encode()).decode()
    )
    return f"v{_ACTIVE_VERSION}:{ciphertext}"


def decrypt_token(ciphertext: str) -> str:
    """Decrypt using the key for the ciphertext's own version prefix.

    Values written under any still-configured version decrypt regardless of the
    current active version — this is what makes rotation non-destructive.
    """
    version, _, body = ciphertext.partition(":")
    if body and _is_version_tag(version):
        return _fernet_for_version(int(version[1:])).decrypt(body.encode()).decode()
    # Legacy: bare Fernet value written before the version stamp existed — it was
    # encrypted with the v1 key, so decrypt with v1 (never the active version).
    return _fernet_for_version(1).decrypt(ciphertext.encode()).decode()


def encrypt_token_at_rest(plaintext: str) -> str:
    """Encrypt a token for at-rest storage when a key is configured.

    Returns ciphertext when ``PENNY_PLAID_TOKEN_KEY`` is set; plaintext
    otherwise — single-player, the local DB file is the user's own and
    encryption at rest is opt-in. Callers must still skip already-encrypted
    values (``is_encrypted``) so re-saves don't double-encrypt.
    """
    if _key_env_for_version(_ACTIVE_VERSION) is not None:
        return encrypt_token(plaintext)
    return plaintext


def encrypt_secret(plaintext: str) -> str:
    """Encrypt an arbitrary secret at rest — generic alias of ``encrypt_token``."""
    return encrypt_token(plaintext)


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a secret written by ``encrypt_secret`` — alias of ``decrypt_token``."""
    return decrypt_token(ciphertext)


def is_encrypted(value: str) -> bool:
    """True for stamped ciphertext or a legacy bare Fernet value.

    Keeps the write path and migration 017 idempotent: already-encrypted
    values are never double-encrypted.
    """
    version, _, body = value.partition(":")
    if body and _is_version_tag(version):
        return True
    return value.startswith(_FERNET_SNIFF)


def _is_version_tag(tag: str) -> bool:
    return len(tag) >= 2 and tag[0] == "v" and tag[1:].isdigit()
