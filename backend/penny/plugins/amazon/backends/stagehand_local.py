"""Stagehand LOCAL backend for Amazon order scraping.

Drives Stagehand's local server — a Node binary packaged inside the
``stagehand`` wheel — against a real Chrome on this machine, so no
Browserbase account or per-minute session cost is involved.

Where the Browserbase backend persists login state in a remote *context*,
this backend persists it in a Chrome user-data directory under the
workspace (one per login profile). The interactive Amazon sign-in is
therefore a one-time cost per profile: later runs reuse the cookies on
disk and go straight to extraction.
"""

from __future__ import annotations

import asyncio
from datetime import date
import importlib
import os
from pathlib import Path
import re

from loguru import logger

from penny.plugins.amazon.backends.dom_asin_reader import (
    LocalAsinReader,
    free_local_port,
)
from penny.plugins.amazon.backends.order_history import (
    ORDERS_URL,
    OrderHarvester,
    is_signed_out,
    navigate,
    wait_for_sign_in,
)
from penny.plugins.amazon.scraper import ScrapedOrder
from penny.workspace import resolve_workspace_dir

# Amazon's order-DETAIL page (unlike the list page) rejects a headless
# session outright: a headless navigation to it redirects to /ap/signin
# demanding a fresh login (openid.pape.max_auth_age=3600), even against a
# profile whose saved session loads that exact page fine when run headed
# two minutes later. Since detail-page itemization is the entire point of
# this backend (P5), headless is not a caller-settable option here — it is
# a hardcoded property of the backend, not a default that can be flipped by
# a future caller. See AGENTS.md / REQUIREMENTS.txt P5 for the same note.
_HEADLESS = False

# How long to leave the visible browser open for a human to sign in, and how
# often to ask the page whether they're done. Signing in is a one-time cost
# per profile (the user-data dir keeps the session), so the poll is cheap in
# aggregate even though each check is an LLM call.
_LOGIN_TIMEOUT_SECONDS = 300
_LOGIN_POLL_SECONDS = 5

# First run pays for unpacking the packaged server binary before it listens.
_SERVER_READY_TIMEOUT_SECONDS = 90.0


def profile_data_dir(profile_key: str | None) -> Path:
    """Chrome user-data directory for ``profile_key``.

    One directory per Amazon login profile, under the workspace, so two
    Amazon accounts never share cookies. ``None`` (no profile in play) gets
    a ``default`` directory.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", profile_key or "default").strip("_")
    return resolve_workspace_dir() / "browser" / "amazon" / (slug or "default")


class StagehandLocalBackend:
    """Amazon scraper backend using Stagehand's LOCAL server.

    The browser is always visible (see ``_HEADLESS``): the first scrape for
    a profile stops on Amazon's sign-in page and waits for the user to
    authenticate, after which the session is kept in
    ``profile_data_dir(profile_key)``.
    """

    def __init__(
        self,
        model_name: str = "google/gemini-2.5-flash",
        model_api_key: str | None = None,
        *,
        profile_key: str | None = None,
    ) -> None:
        """Initialize the Stagehand LOCAL backend.

        Args:
            model_name: LLM model for Stagehand. Defaults to Gemini Flash.
            model_api_key: API key for the model. If None, reads from
                MODEL_API_KEY or GOOGLE_API_KEY environment variable.
            profile_key: Amazon login profile this scrape belongs to; selects
                the Chrome user-data directory holding its session.

        There is deliberately no ``headless`` parameter: see the module-level
        ``_HEADLESS`` constant for why headless is a hardcoded property of
        this backend rather than something a caller can set.
        """
        self._model_name = model_name
        self._model_api_key = model_api_key or os.getenv(
            "MODEL_API_KEY", os.getenv("GOOGLE_API_KEY", "")
        )
        self._user_data_dir = profile_data_dir(profile_key)
        self._harvester = OrderHarvester()

    @property
    def collected_orders(self) -> list[ScrapedOrder]:
        """Orders collected so far (used for partial recovery on failure)."""
        return self._harvester.orders

    def scrape_order_history(
        self,
        *,
        since: date | None = None,
        until: date | None = None,
        max_orders: int | None = None,
        fetch_item_details: bool = True,
    ) -> list[ScrapedOrder]:
        """Scrape Amazon order history via Stagehand's local server.

        Args:
            since: Inclusive lower bound on ``order_date``.
            until: Inclusive upper bound on ``order_date``.
            max_orders: Optional maximum orders across all visited years.
            fetch_item_details: Whether to fetch each order's detail page for
                real per-item data (see ``AmazonScraperBackend``).

        Returns:
            List of ScrapedOrder objects.
        """
        logger.info(
            "Local scrape_order_history start: since={} until={} max_orders={} "
            "fetch_item_details={} user_data_dir={}",
            since,
            until,
            max_orders,
            fetch_item_details,
            self._user_data_dir,
        )
        # See AmazonScraperBackend for why this owns its own event loop.
        return asyncio.run(
            self._scrape_order_history_async(
                since=since,
                until=until,
                max_orders=max_orders,
                fetch_item_details=fetch_item_details,
            )
        )

    async def _scrape_order_history_async(
        self,
        *,
        since: date | None,
        until: date | None,
        max_orders: int | None,
        fetch_item_details: bool,
    ) -> list[ScrapedOrder]:
        """Async implementation of order history scraping."""
        try:
            stagehand_module = importlib.import_module("stagehand")
        except ImportError as e:
            raise ImportError(
                "Stagehand is not installed. Install with: pip install stagehand"
            ) from e

        if not self._model_api_key:
            raise ValueError(
                "MODEL_API_KEY (or GOOGLE_API_KEY) is required for the local "
                "Stagehand backend"
            )

        # The local server writes Chrome's stdio logs into the user-data dir
        # and does not create it, so an absent directory fails session start.
        self._user_data_dir.mkdir(parents=True, exist_ok=True)

        # A fixed, known CDP port lets LocalAsinReader attach a second,
        # read-only Playwright client to this same Chrome afterward — see
        # dom_asin_reader.py. Chosen fresh per scrape (not hardcoded) so
        # nothing here can collide with another local dev server.
        cdp_port = free_local_port()

        client = stagehand_module.AsyncStagehand(
            server="local",
            model_api_key=self._model_api_key,
            local_headless=_HEADLESS,
            local_ready_timeout_s=_SERVER_READY_TIMEOUT_SECONDS,
        )
        logger.info("Starting local Stagehand session (model={})", self._model_name)
        session = await client.sessions.start(
            model_name=self._model_name,
            browser={
                "type": "local",
                "launch_options": {
                    "headless": _HEADLESS,
                    "user_data_dir": str(self._user_data_dir),
                    "preserve_user_data_dir": True,
                    "port": cdp_port,
                },
            },
        )
        logger.info("Local Stagehand session started: {}", session.id)

        # Best-effort: a failed attach just means no real-ASIN capability
        # this run (LocalAsinReader.connect() logs why and returns False),
        # never a reason to abort the scrape.
        asin_reader = LocalAsinReader(cdp_port)
        asin_reader_attached = await asin_reader.connect()

        try:
            await self._ensure_signed_in(session)
            return await self._harvester.harvest(
                session,
                since=since,
                until=until,
                max_orders=max_orders,
                fetch_item_details=fetch_item_details,
                link_reader=(
                    asin_reader.read_current_page_links
                    if asin_reader_attached
                    else None
                ),
            )
        finally:
            await asin_reader.close()
            logger.info("Closing local Stagehand session and browser")
            try:
                await session.end()
            finally:
                await client.close()

    async def _ensure_signed_in(self, session: object) -> None:
        """Open the orders page, waiting for a human sign-in when required.

        Amazon redirects an unauthenticated request for the orders page to
        ``/ap/signin``. The browser is visible, so the user completes the
        sign-in there and Amazon returns them to the orders page; the stored
        user-data dir means later runs skip this entirely.
        """
        landed = await navigate(session, ORDERS_URL)
        if not is_signed_out(landed):
            logger.info("Amazon orders page reached; already signed in")
            return

        logger.warning(
            "Amazon requires sign-in. Complete the login in the open browser "
            "window within {}s; the session is then saved to {}",
            _LOGIN_TIMEOUT_SECONDS,
            self._user_data_dir,
        )
        await wait_for_sign_in(
            session,
            timeout_seconds=_LOGIN_TIMEOUT_SECONDS,
            poll_seconds=_LOGIN_POLL_SECONDS,
        )
