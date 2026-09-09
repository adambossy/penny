"""Stagehand BROWSERBASE backend for Amazon order scraping.

Runs the scrape in a Browserbase cloud browser instead of a local one, which
is what makes an unattended scrape possible: login state lives in a
Browserbase *context* (server-side), not on this machine's disk, so a
headless or scheduled run can reuse a session a human established earlier —
and several accounts can each hold their own live context at once.

Signing in happens through Browserbase's Session Live View: the run logs a
URL, the user opens it in their own browser, authenticates there, and the
cookies land in the context.
"""

from __future__ import annotations

import asyncio
from datetime import date
import importlib
import os
from typing import Any

from loguru import logger

from penny.plugins.amazon.backends.order_history import (
    ORDERS_URL,
    OrderHarvester,
    is_signed_out,
    navigate,
    wait_for_sign_in,
)
from penny.plugins.amazon.scraper import ScrapedOrder

# Browserbase per-session lifetime cap (seconds). Default is ~5 min on free
# plans, which is too short for multi-page year scrapes (~30s/page). Bumped so
# a single year of orders can complete in one session without retry-thrashing.
_DEFAULT_SESSION_TIMEOUT_SECONDS = 1800

_LOGIN_TIMEOUT_SECONDS = 300
_LOGIN_POLL_SECONDS = 5


class StagehandBrowserbaseBackend:
    """Amazon scraper backend using Stagehand with Browserbase.

    Requires BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID. For
    authenticated scraping, use Browserbase contexts to persist login state:

    1. Create a context with ``create_context()``
    2. Log in manually via Session Live View (``login_mode=True``)
    3. Reuse the context_id for future scraping sessions
    """

    def __init__(
        self,
        model_name: str = "google/gemini-2.5-flash",
        model_api_key: str | None = None,
        browserbase_api_key: str | None = None,
        browserbase_project_id: str | None = None,
        context_id: str | None = None,
        persist_context: bool = True,
        login_mode: bool = False,
    ) -> None:
        """Initialize the Stagehand Browserbase backend.

        Args:
            model_name: LLM model for Stagehand. Defaults to Gemini Flash.
            model_api_key: API key for the model. If None, reads from
                MODEL_API_KEY or GOOGLE_API_KEY environment variable.
            browserbase_api_key: Browserbase API key. If None, reads from
                BROWSERBASE_API_KEY environment variable.
            browserbase_project_id: Browserbase project ID. If None, reads from
                BROWSERBASE_PROJECT_ID environment variable.
            context_id: Optional Browserbase context ID for session persistence.
                Use `create_context()` to create a new context.
            persist_context: Whether to persist session changes to context.
                Defaults to True. Set to False for read-only access.
            login_mode: If True, wait for manual login via Session Live View
                instead of erroring when login is required. Use for first-time
                authentication setup.
        """
        self._model_name = model_name
        self._model_api_key = model_api_key or os.getenv(
            "MODEL_API_KEY", os.getenv("GOOGLE_API_KEY", "")
        )
        self._browserbase_api_key = browserbase_api_key or os.getenv(
            "BROWSERBASE_API_KEY", ""
        )
        self._browserbase_project_id = browserbase_project_id or os.getenv(
            "BROWSERBASE_PROJECT_ID", ""
        )
        self._context_id = context_id
        self._persist_context = persist_context
        self._login_mode = login_mode
        self._harvester = OrderHarvester()

    @property
    def collected_orders(self) -> list[ScrapedOrder]:
        """Orders collected so far (used for partial recovery on failure)."""
        return self._harvester.orders

    @classmethod
    def create_context(
        cls,
        browserbase_api_key: str | None = None,
        browserbase_project_id: str | None = None,
    ) -> str:
        """Create a new Browserbase context for session persistence.

        Contexts persist cookies, localStorage, and session tokens across
        browser sessions. Create a context once, then reuse its ID for
        future scraping sessions.

        Workflow:
        1. Call create_context() to get a context_id
        2. Create a backend with that context_id
        3. Start an interactive session to log in (use Session Live View)
        4. Future sessions with same context_id will be pre-authenticated

        Args:
            browserbase_api_key: Browserbase API key. If None, reads from
                BROWSERBASE_API_KEY environment variable.
            browserbase_project_id: Browserbase project ID. If None, reads from
                BROWSERBASE_PROJECT_ID environment variable.

        Returns:
            Context ID string to use in future sessions.

        Raises:
            ImportError: If browserbase package is not installed.
            ValueError: If API credentials are missing.
        """
        try:
            browserbase_module = importlib.import_module("browserbase")
        except ImportError as e:
            raise ImportError(
                "browserbase package not installed. "
                "Install with: pip install browserbase"
            ) from e

        api_key = browserbase_api_key or os.getenv("BROWSERBASE_API_KEY", "")
        project_id = browserbase_project_id or os.getenv("BROWSERBASE_PROJECT_ID", "")

        if not api_key:
            raise ValueError("BROWSERBASE_API_KEY is required")
        if not project_id:
            raise ValueError("BROWSERBASE_PROJECT_ID is required")

        client = browserbase_module.Browserbase(api_key=api_key)
        context = client.contexts.create(project_id=project_id)
        return str(context.id)

    def get_session_live_view_url(self, session_id: str) -> str:
        """Get the Live View URL for interactive session access.

        Use this URL to manually log in to Amazon through the browser.

        Args:
            session_id: Browserbase session ID from a running session.

        Returns:
            URL to open in your browser for interactive access.
        """
        return f"https://www.browserbase.com/sessions/{session_id}"

    def login(self) -> None:
        """Establish this context's Amazon session via Session Live View.

        Opens a session on the configured context, parks on Amazon's sign-in
        page, and waits for the user to authenticate through the Live View
        URL it logs. The cookies land in the context, so later scrapes on the
        same context start already signed in.
        """
        logger.info(
            "Browserbase login flow start: context_id_set={}",
            self._context_id is not None,
        )
        asyncio.run(self._login_async())

    async def _login_async(self) -> None:
        """Async implementation of the interactive login flow."""
        client, session, live_view_url = await self._open_session()
        try:
            await self._ensure_signed_in(session, live_view_url)
        finally:
            logger.info("Closing Browserbase session")
            try:
                await session.end()
            finally:
                await client.close()

    def scrape_order_history(
        self,
        *,
        since: date | None = None,
        until: date | None = None,
        max_orders: int | None = None,
        fetch_item_details: bool = True,
    ) -> list[ScrapedOrder]:
        """Scrape Amazon order history via Stagehand Browserbase.

        Args:
            since: Inclusive lower bound on ``order_date`` (already DB-floored
                by orchestrator).
            until: Inclusive upper bound on ``order_date``.
            max_orders: Optional maximum orders across all visited years.
            fetch_item_details: Whether to fetch each order's detail page for
                real per-item data (see ``AmazonScraperBackend``). Costs one
                extra navigation + extraction per order on a per-minute
                billed session; ``max_orders`` still bounds the total.

        Returns:
            List of ScrapedOrder objects.
        """
        logger.info(
            "Browserbase scrape_order_history start: since={} until={} "
            "max_orders={} fetch_item_details={} context_id_set={} login_mode={}",
            since,
            until,
            max_orders,
            fetch_item_details,
            self._context_id is not None,
            self._login_mode,
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
        client, session, live_view_url = await self._open_session()

        try:
            await self._ensure_signed_in(session, live_view_url)
            return await self._harvester.harvest(
                session,
                since=since,
                until=until,
                max_orders=max_orders,
                fetch_item_details=fetch_item_details,
            )
        finally:
            logger.info("Closing Browserbase session")
            try:
                await session.end()
            finally:
                await client.close()

    async def _open_session(self) -> tuple[Any, Any, str]:
        """Start a Browserbase session; return (client, session, live view URL)."""
        try:
            stagehand_module = importlib.import_module("stagehand")
        except ImportError as e:
            raise ImportError(
                "Stagehand is not installed. Install with: pip install stagehand"
            ) from e

        if not self._browserbase_api_key:
            raise ValueError(
                "BROWSERBASE_API_KEY environment variable is required for "
                "Browserbase backend"
            )
        if not self._browserbase_project_id:
            raise ValueError(
                "BROWSERBASE_PROJECT_ID environment variable is required for "
                "Browserbase backend"
            )
        if not self._model_api_key:
            raise ValueError(
                "MODEL_API_KEY (or GOOGLE_API_KEY) is required for the "
                "Browserbase backend"
            )

        client = stagehand_module.AsyncStagehand(
            browserbase_api_key=self._browserbase_api_key,
            browserbase_project_id=self._browserbase_project_id,
            model_api_key=self._model_api_key,
        )
        logger.info("Starting Browserbase Stagehand session")
        session = await client.sessions.start(
            model_name=self._model_name,
            browser={"type": "browserbase"},
            browserbase_session_create_params=self._session_create_params(),
        )
        live_view_url = self.get_session_live_view_url(session.data.session_id)
        logger.info(
            "Browserbase session {} started (live view: {})",
            session.data.session_id,
            live_view_url,
        )
        return client, session, live_view_url

    def _session_create_params(self) -> dict[str, Any]:
        """Browserbase session params: lifetime cap plus the login context."""
        params: dict[str, Any] = {"timeout": _DEFAULT_SESSION_TIMEOUT_SECONDS}
        if self._context_id:
            params["browserSettings"] = {
                "context": {"id": self._context_id, "persist": self._persist_context}
            }
            logger.info(
                "Using Browserbase context_id={} (persist={}, timeout={}s)",
                self._context_id,
                self._persist_context,
                _DEFAULT_SESSION_TIMEOUT_SECONDS,
            )
        else:
            logger.info(
                "No Browserbase context ID configured (timeout={}s)",
                _DEFAULT_SESSION_TIMEOUT_SECONDS,
            )
        return params

    async def _ensure_signed_in(self, session: Any, live_view_url: str) -> None:
        """Open the orders page, handling Amazon's sign-in redirect.

        In ``login_mode`` the run parks on the sign-in page and waits for the
        user to authenticate through the Live View; otherwise a sign-in
        redirect means the context is missing or expired, which is an error
        the caller has to resolve rather than something to wait out.
        """
        landed = await navigate(session, ORDERS_URL)
        if not is_signed_out(landed):
            logger.info("Amazon orders page reached; context is authenticated")
            return

        if not self._login_mode:
            if self._context_id:
                raise RuntimeError(
                    "Amazon login required despite using a context. "
                    "The context may have expired. Please:\n"
                    "1. Clear it with clear_amazon_login_context()\n"
                    "2. Re-run so a fresh context is created and logged in\n"
                )
            raise RuntimeError(
                "Amazon login required. To use Browserbase:\n"
                "1. Create a context: "
                "ctx_id = StagehandBrowserbaseBackend.create_context()\n"
                "2. Run in login_mode to authenticate via Session Live View\n"
                "3. Reuse the same context_id for automated scraping"
            )

        logger.warning(
            "Amazon requires sign-in. Open the Browserbase Live View and log "
            "in within {}s: {}",
            _LOGIN_TIMEOUT_SECONDS,
            live_view_url,
        )
        await wait_for_sign_in(
            session,
            timeout_seconds=_LOGIN_TIMEOUT_SECONDS,
            poll_seconds=_LOGIN_POLL_SECONDS,
        )
