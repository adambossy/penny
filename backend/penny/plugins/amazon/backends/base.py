"""Base protocol for Amazon scraper backends."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from penny.plugins.amazon.scraper import ScrapedOrder


class AmazonScraperBackend(Protocol):
    """Protocol for Amazon scraper backends.

    All backends must implement scrape_order_history to return a list of
    ScrapedOrder objects. The main scraper tool handles database persistence.

    Year-by-year navigation, when needed, is the implementation's concern.
    Callers pass only the date window (since/until) plus an optional cap.

    ``scrape_order_history`` is a synchronous entry point over an
    async-internally implementation: callers reach backends off the event
    loop (the ``@tool`` wrappers use ``asyncio.to_thread``), so each
    implementation owns its own ``asyncio.run()`` call rather than assuming
    a loop is already running. This keeps the async plumbing to a single
    well-defined entry point per backend.
    """

    def scrape_order_history(
        self,
        *,
        since: date | None = None,
        until: date | None = None,
        max_orders: int | None = None,
        fetch_item_details: bool = True,
    ) -> list[ScrapedOrder]:
        """Scrape Amazon order history within an inclusive date window.

        Args:
            since: Inclusive lower bound on ``order_date``. ``None`` means no
                lower bound. Already DB-floored by the orchestrator before it
                reaches the backend.
            until: Inclusive upper bound on ``order_date``. ``None`` means no
                upper bound.
            max_orders: Optional maximum number of orders to scrape across all
                pages/years. ``None`` means scrape everything that matches.
            fetch_item_details: Whether to fetch each order's detail page for
                real per-item price/ASIN/quantity and order-level tax/
                shipping (the list page never carries them). Costs one extra
                navigation + extraction per order; ``max_orders`` still
                bounds the total.

        Returns:
            List of ScrapedOrder objects with order details and items.
        """
        ...
