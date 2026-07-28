"""Hosted Plaid Link: link-token creation + server-side public-token exchange.

Rearchitects the localhost Link flow (``connect_new_account``) for remote
deployment (productionization B-6): the ``react-plaid-link`` picker runs in the
browser and the ``public_token`` is exchanged **server-side** here. The localhost
flow stays for dev behind ``PENNY_PLAID_LINK_MODE=localhost``.

``create_link_token`` mints a token for the local user;
``exchange_public_token`` exchanges it, encrypts the access token at rest, writes
the finance ``PlaidItem`` + ``PlaidAccount`` rows, kicks off the first sync, and
enqueues a ``plaid_link`` success reminder. All external collaborators (Plaid client, sync, reminder queue)
are injectable so tests drive the flow with fakes and no real Plaid.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import os
import threading
from typing import Any, Protocol
import uuid

from loguru import logger

from penny.adapters.clients.plaid import PlaidClient
from penny.adapters.db.models import PlaidAccount, PlaidItem
from penny.security.token_cipher import encrypt_token_at_rest


class _ReminderQueue(Protocol):
    async def enqueue(
        self, session_id: str, kind: str, content: str, *, override: bool = True
    ) -> None: ...


def _link_mode() -> str:
    return os.environ.get("PENNY_PLAID_LINK_MODE", "hosted").strip().lower() or "hosted"


def create_link_token(
    *,
    user_id: uuid.UUID,
    access_token: str | None = None,
    client: PlaidClient | None = None,
) -> dict[str, Any]:
    """Create a Plaid ``link_token`` for ``user_id``.

    New-link mode (no ``access_token``) returns ``{mode: 'hosted', link_token,
    expiration}`` for the inline connect card. Passing an existing item's
    ``access_token`` mints an **update-mode** token (Plaid re-auth / relink) and
    returns ``{mode: 'update', ...}`` — the same ``PlaidLinkCard`` renders it, but
    onSuccess restores the existing item rather than exchanging a new one.

    Localhost dev mode returns a pointer to the ``connect_new_account`` flow
    instead — no token is minted, and relink isn't supported there.
    """
    if _link_mode() == "localhost":
        return {
            "mode": "localhost",
            "instructions": (
                "Local dev uses the localhost Plaid Link flow — call "
                "connect_new_account to open the browser picker."
            ),
        }
    client = client or PlaidClient.from_env()
    redirect_uri = os.environ.get("PLAID_REDIRECT_URI", "").strip() or None
    update_mode = access_token is not None
    link_token = client.create_link_token(
        user_id=str(user_id),
        redirect_uri=redirect_uri,
        # Update mode carries no products (Plaid rejects them); the client forces
        # the list empty when an access_token is present.
        products=None if update_mode else ["transactions"],
        access_token=access_token,
    )
    mode = "update" if update_mode else "hosted"
    return {"mode": mode, "link_token": link_token, "expiration": None}


async def exchange_public_token(
    session: Any,
    *,
    public_token: str,
    conversation_id: str,
    queue: _ReminderQueue,
    client: Any = None,
    sync: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Exchange a ``public_token`` and persist the linked item + accounts.

    Encrypts the access token at rest, inserts the ``PlaidItem`` and its
    ``PlaidAccount`` rows, fires the first sync (fire-and-forget), and enqueues
    a ``plaid_link`` reminder so the next turn relays the success. Returns
    ``{item_id, accounts}``.

    ``queue`` is injected by the website runtime (the concrete web-backed
    ``DbReminderQueue`` lives in the website persistence package) — the agent
    domain never imports it, keeping agent/website segregation intact.
    """
    client = client or PlaidClient.from_env()
    exch = await asyncio.to_thread(client.item_public_token_exchange, public_token)
    access_token = exch["access_token"]
    item_id = exch["item_id"]

    accounts_resp = await asyncio.to_thread(client.accounts_get, access_token)
    accounts = accounts_resp.get("accounts") or []
    institution = (accounts_resp.get("item") or {}).get(
        "institution_name"
    ) or "Your bank"

    # At-rest encryption when PENNY_PLAID_TOKEN_KEY is configured (opt-in
    # single-player; the local DB file is the user's own).
    stored_token = encrypt_token_at_rest(access_token)
    session.add(
        PlaidItem(
            item_id=item_id,
            access_token=stored_token,
            institution_name=institution,
        )
    )
    # Flush the item before its accounts so the FK (accounts.item_id →
    # plaid_items) resolves under SQLite's enforced foreign keys.
    session.flush()
    for account in accounts:
        session.add(
            PlaidAccount(
                account_id=account["account_id"],
                item_id=item_id,
                name=account.get("name"),
            )
        )
    session.flush()

    # First sync: fire-and-forget so the exchange returns immediately.
    (sync or _default_sync())(item_id)

    content = (
        f"{institution} linked ({len(accounts)} accounts; first sync started). "
        "Tell the user, and mention they can connect more accounts anytime just "
        "by asking."
    )
    await queue.enqueue(conversation_id, "plaid_link", content)
    return {"item_id": item_id, "accounts": len(accounts)}


def _default_sync() -> Callable[[str], None]:
    """Return a fire-and-forget first-sync callable.

    Runs the existing sync service in a daemon thread (its own event loop) so a
    linked item pulls transactions without blocking the exchange response.
    Best-effort: failures are logged, never raised.
    """

    def _kick(item_id: str) -> None:
        def _run() -> None:
            from penny.db import get_db
            from penny.services import build_categorizer, get_taxonomy
            from penny.tools._services.sync_service import SyncTool

            try:
                tool = SyncTool(
                    plaid_client=PlaidClient.from_env(),
                    categorizer_factory=build_categorizer,
                    db=get_db(),
                    taxonomy=get_taxonomy(),
                )
                asyncio.run(tool.sync())
            except Exception:
                logger.exception("first sync after link failed for item {}", item_id)

        threading.Thread(target=_run, daemon=True).start()

    return _kick
