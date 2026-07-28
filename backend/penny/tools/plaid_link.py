"""Agent tool: offer an inline hosted Plaid Link card.

Returns the link-token structured content the frontend ``PlaidLinkCard`` renderer
consumes (generative UI). The server-side ``public_token`` exchange is NOT here —
it is a website route (``POST /api/plaid/exchange``) the card calls on success,
keeping the token exchange and finance writes off the agent surface.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_harness import tool

from penny.db import get_db
from penny.identity import local_user_id
from penny.tools._services.plaid_link import create_link_token


@tool
async def connect_bank_account() -> dict[str, Any]:
    """Offer the user an inline card to connect a bank account via Plaid.

    Use when the user has no bank linked, or asks to connect (or add) one.
    Returns the Plaid link-token payload; the inline card handles the secure
    Plaid Link flow and the server exchanges the token. Tell the user a connect
    card is shown right in the chat.
    """
    return await asyncio.to_thread(create_link_token, user_id=local_user_id())


@tool
async def relink_account(item_id: str) -> dict[str, Any]:
    """Offer an inline card to re-authenticate (relink) an existing connection.

    Use when a connection needs re-authentication — e.g. ``plaid_connection_status``
    reports ``needs_relink`` for it, or a sync surfaced ``ITEM_LOGIN_REQUIRED``.
    Pass that connection's ``item_id``. Mints an **update-mode** Plaid link token
    and returns it as an inline card; completing it restores the *same* item (no
    new connection), and syncs resume. Tell the user a relink card is shown in the
    chat.

    Returns the link-token payload ``{mode: 'update', link_token, item_id,
    institution_name}``, or ``{status: 'error', message}`` when the item is
    unknown to this user.
    """

    def _mint() -> dict[str, Any]:
        item = get_db().get_plaid_item(item_id)
        if item is None:
            return {
                "status": "error",
                "message": f"No linked connection found for item_id {item_id!r}.",
            }
        payload = create_link_token(
            user_id=local_user_id(), access_token=item.access_token
        )
        payload["item_id"] = item.item_id
        payload["institution_name"] = getattr(item, "institution_name", None)
        return payload

    return await asyncio.to_thread(_mint)
