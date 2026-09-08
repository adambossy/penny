"""Playwriter MCP backend for Amazon order scraping."""

from __future__ import annotations

from datetime import date

from agents import Agent, Runner
from agents.mcp import MCPServerStdio, MCPServerStdioParams
from loguru import logger

from penny.plugins.amazon.scraper import ScrapedOrder, ScrapeResult


class PlaywriterBackend:
    """Amazon scraper backend using Playwriter MCP.

    This backend uses the Playwriter browser extension via MCP to scrape
    Amazon order history. Requires user to have the browser open with
    Playwriter extension activated.
    """

    def __init__(self, model: str = "gpt-4o") -> None:
        """Initialize the Playwriter backend.

        Args:
            model: LLM model to use for the scraper agent. Defaults to gpt-4o.
        """
        self._model = model

    def scrape_order_history(
        self,
        *,
        since: date | None = None,
        until: date | None = None,
        max_orders: int | None = None,
        fetch_item_details: bool = True,
    ) -> list[ScrapedOrder]:
        """Scrape Amazon order history via Playwriter MCP.

        Args:
            since: Inclusive lower bound on ``order_date``.
            until: Inclusive upper bound on ``order_date``.
            max_orders: Optional maximum orders to scrape.
            fetch_item_details: Accepted for ``AmazonScraperBackend``
                interface parity; unused here. This backend runs a single
                agent turn that already extracts item data inline from
                whatever the page shows it, with no separate list/detail
                phases to toggle.

        Returns:
            List of ScrapedOrder objects.
        """
        logger.info(
            "Playwriter scrape starting: since={} until={} max_orders={}",
            since,
            until,
            max_orders,
        )

        if since is not None and until is not None:
            window_instruction = (
                f"for orders dated on or after {since.isoformat()} and "
                f"on or before {until.isoformat()}"
            )
        elif since is not None:
            window_instruction = f"for orders dated on or after {since.isoformat()}"
        elif until is not None:
            window_instruction = f"for orders dated on or before {until.isoformat()}"
        else:
            window_instruction = "for the past year"

        limit_instruction = (
            f"Stop after scraping {max_orders} orders."
            if max_orders
            else "Scrape all available orders that match the date window."
        )

        instructions = f"""
        Navigate to https://www.amazon.com/your-orders/orders

        Scrape Amazon purchase history {window_instruction}. Amazon's UI only
        shows the past 3 months by default — use the time-filter dropdown
        (or the ?timeFilter=year-YYYY URL parameter) to iterate year-by-year
        as needed to cover the requested window.

        For each order:
        1. Extract order ID (e.g., "113-5524816-2451403")
        2. Extract order date (YYYY-MM-DD format)
        3. Extract order total, tax, and shipping (convert to cents, $49.77 -> 4977)
        4. For each item: extract ASIN (from URL), description, price (cents), quantity

        Handle pagination - click "Next" to load more orders.
        {limit_instruction}

        Return ALL scraped data as structured JSON matching the output schema.
        """

        playwriter_server = MCPServerStdio(
            params=MCPServerStdioParams(command="npx", args=["playwriter"]),
            name="playwriter",
        )

        agent = Agent(
            name="AmazonScraper",
            instructions=instructions,
            model=self._model,
            mcp_servers=[playwriter_server],
            output_type=ScrapeResult,
        )

        try:
            scrape_result = Runner.run_sync(agent, "Begin scraping Amazon orders")
        except Exception:
            logger.exception("Playwriter scrape run_sync failed")
            raise

        final_output = scrape_result.final_output
        if final_output is None:
            logger.warning("Playwriter scrape completed with no final output")
            return []

        orders = list(final_output.orders)
        logger.info("Playwriter scrape completed: orders={}", len(orders))
        return orders
