"""The single player's stable local identity.

Single-player Penny still needs a *user reference* — Plaid link tokens want a
stable ``client_user_id`` — but there is no ``users`` table. The reference is
a UUID minted once into the workspace (``user_id`` file) and reused forever;
``penny init`` creates it, and any code path that needs it first touches it
into existence, so the value never depends on call order.
"""

from __future__ import annotations

from pathlib import Path
import uuid

from penny.workspace import resolve_workspace_dir


def local_user_id() -> uuid.UUID:
    """The workspace's stable user UUID, minted on first use."""
    path = _user_id_path()
    if path.exists():
        return uuid.UUID(path.read_text(encoding="utf-8").strip())
    value = uuid.uuid4()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")
    return value


def _user_id_path() -> Path:
    return resolve_workspace_dir() / "user_id"
