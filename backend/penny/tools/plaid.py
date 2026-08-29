"""Plaid-facing tools.

The Plaid Link flow currently runs locally on ``https://localhost:8443``
and pops the user's browser via ``webbrowser.open``. This works for dev on
the maintainer's machine but is incompatible with a remote sandbox — the
productionization plan ([[productionize-transactoid]] B-6) covers
re-architecting the OAuth callback through a centralized public endpoint.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_harness import tool

from penny.adapters.clients.plaid import PlaidClient, PlaidClientError
from penny.db import get_db


def _item_to_dict(item: Any) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "institution_id": getattr(item, "institution_id", None),
        "institution_name": getattr(item, "institution_name", None),
        "sync_cursor": getattr(item, "sync_cursor", None),
        "created_at": item.created_at.isoformat()
        if getattr(item, "created_at", None)
        else None,
    }


@tool
async def list_plaid_accounts() -> dict[str, Any]:
    """List every Plaid item (bank/card connection) the user has linked.

    Returns ``{"items": [...], "count": N}``. Empty when nothing is connected.
    """

    def _fetch() -> list[Any]:
        return get_db().list_plaid_items()

    items = await asyncio.to_thread(_fetch)
    return {"items": [_item_to_dict(it) for it in items], "count": len(items)}


# Plaid error_codes that mean the user must re-authenticate (relink) the item
# before syncs resume. Mirrors sync_service._PLAID_RELINK_CODES.
_RELINK_CODES = frozenset(
    {
        "ITEM_LOGIN_REQUIRED",
        "PENDING_EXPIRATION",
        "INVALID_CREDENTIALS",
        "INVALID_MFA",
        "ITEM_LOCKED",
        "USER_PERMISSION_REVOKED",
    }
)


@tool
async def plaid_connection_status() -> dict[str, Any]:
    """Check the live health of every linked bank/card connection.

    Queries Plaid on demand (``/item/get`` per item) and reports which
    connections are healthy versus which need the user to re-authenticate
    (relink) — e.g. an expired login (``ITEM_LOGIN_REQUIRED``). Use this before
    telling the user their data is complete, or when a sync looked short. When a
    connection ``needs_relink``, offer the ``relink_account`` card for its
    ``item_id``.

    Returns ``{"connections": [{item_id, institution_name, healthy, error_code,
    needs_relink}], "needs_relink": [institution names], "count": N}``.
    """

    def _check() -> list[dict[str, Any]]:
        db = get_db()
        items = db.list_plaid_items()
        client = PlaidClient.from_env() if items else None
        out: list[dict[str, Any]] = []
        for item in items:
            name = getattr(item, "institution_name", None) or item.item_id
            try:
                error_code = client.get_item_status(item.access_token)  # type: ignore[union-attr]
            except PlaidClientError as exc:
                # An /item/get failure is itself a health signal — surface it as a
                # relink candidate rather than hiding the connection.
                error_code = _error_code_from(str(exc))
            needs_relink = bool(error_code) and error_code in _RELINK_CODES
            out.append(
                {
                    "item_id": item.item_id,
                    "institution_name": name,
                    "healthy": error_code is None,
                    "error_code": error_code,
                    "needs_relink": needs_relink,
                }
            )
        return out

    connections = await asyncio.to_thread(_check)
    needs = [c["institution_name"] for c in connections if c["needs_relink"]]
    return {
        "connections": connections,
        "needs_relink": needs,
        "count": len(connections),
    }


def _error_code_from(message: str) -> str | None:
    """Extract a known relink error_code embedded in a Plaid error message."""
    for code in _RELINK_CODES:
        if code in message:
            return code
    return None


@tool
async def connect_new_account() -> dict[str, Any]:
    """Connect a new bank/card via Plaid Link.

    Opens a local browser to the Plaid Link flow, waits for the user to
    finish OAuth, exchanges the public token for an access token, and
    persists the connection. Blocks until the user completes the flow (or
    a timeout fires inside the client). Tell the user to expect a browser
    window.

    Returns ``{"status": "success", "item_id", "institution_name", ...}``
    or ``{"status": "error", "message"}`` on failure.
    """

    def _connect() -> dict[str, Any]:
        try:
            plaid_client = PlaidClient.from_env()
            return plaid_client.connect_new_account(db=get_db())
        except Exception as exc:
            return {"status": "error", "message": f"Failed to connect account: {exc}"}

    return await asyncio.to_thread(_connect)
