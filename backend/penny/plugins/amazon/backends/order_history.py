"""Amazon's order-history pages, driven through an open Stagehand session.

Everything here is backend-agnostic: it takes a started Stagehand session
(local Chrome or Browserbase — the SDK's session object is the same either
way) and walks the order-history view with it. What differs between the
backends is how a session is *obtained* and how a human signs in; that
lives in the backend modules, and this module holds the rest.

The order-history LIST page (``ORDERS_URL``) only ever carries order id,
date, and order total — Amazon does not render per-item price, ASIN,
quantity, tax, or shipping there. Real per-item data lives on each order's
DETAIL page (``detail_url()``), which is why ``OrderHarvester`` fetches it
per order rather than trying to squeeze it out of the list view.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
import hashlib
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, field_validator

from penny.plugins.amazon.scraper import ScrapedItem, ScrapedOrder

# A no-argument async callable that reads the real ASINs off whatever page a
# session currently has open (see backends/dom_asin_reader.py). ``None``
# means "no real-ASIN capability this run" — the local backend supplies one
# when it can attach Playwright over CDP; every other backend passes
# ``None`` and the synthetic item identity below is used unconditionally,
# same as before this capability existed.
AsinReader = Callable[[], Awaitable[list[str]]]

ORDERS_URL = "https://www.amazon.com/your-orders/orders"
DETAIL_URL_BASE = "https://www.amazon.com/gp/your-account/order-details"

PageOutcome = Literal["continue", "limit_hit", "past_floor"]

# Maximum backward-iteration window when no `since` is provided. Amazon's
# year-filter dropdown lists years back to ~2010; this keeps iteration bounded
# even when the orchestrator's DB-derived floor is absent.
_DEFAULT_FLOOR_YEARS = 20

_ORDERS_PER_PAGE = 10

NAV_TIMEOUT_MS = 60_000
# Extraction of a full orders page runs well past the SDK's 60s default.
EXTRACT_TIMEOUT_SECONDS = 180.0
_PAGE_SETTLE_SECONDS = 2

_EXTRACT_INSTRUCTION = (
    "Extract all orders visible on this page. "
    "For each order, get the order ID, "
    "date (YYYY-MM-DD format), and total amount in cents. "
    "This list view does not show a reliable per-item price, ASIN, or "
    "quantity — leave item fields blank/zero rather than guessing; the "
    "detail page is fetched separately for that data. "
    "Also check if there's a 'Next' link."
)

_DETAIL_EXTRACT_INSTRUCTION = (
    "Extract the itemized detail for this Amazon order. For each line item, "
    "get its ASIN (leave blank if none is shown, e.g. for a digital order "
    "or a gift-wrap/service line), its description, its UNIT price in cents "
    "(the price for one unit, not price times quantity), and the quantity "
    "ordered. Also get the order-level tax and shipping, both in cents."
)

# Tokens the LLM sometimes emits in place of a real ASIN when the detail page
# genuinely doesn't show one (digital goods, gift wrap, promotions). Treated
# as "no ASIN" rather than as a literal value.
_BLANK_ASIN_TOKENS = frozenset({"", "null", "none", "n/a", "na", "unknown"})


def _clean_asin(value: object) -> str:
    """Normalize an extracted ASIN, mapping filler tokens to ``""``.

    Observed on real runs: the LLM fills a missing ASIN with the empty
    string, the literal word "null", or similar — never a signal that a
    genuine ASIN happens to be that value. Non-string input (extraction
    occasionally hands back ``None``) also becomes ``""``.
    """
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    return "" if cleaned.lower() in _BLANK_ASIN_TOKENS else cleaned


def _clamp_non_negative(value: int) -> int:
    """Floor a cents amount at 0.

    The LLM has been observed inventing ``-1`` as a "missing value" filler
    for prices/tax/shipping. A negative amount is never legitimate here, and
    the splitter's proportional allocation requires non-negative inputs; 0
    ("nothing extracted") is the safe substitute.
    """
    return value if value >= 0 else 0


def _clamp_positive_quantity(value: int) -> int:
    """Floor a quantity at 1.

    Every line item on a real order represents at least one unit — a
    non-positive quantity is always extraction noise (the same ``-1``-filler
    behavior seen on prices), never a genuine order state.
    """
    return value if value > 0 else 1


class ExtractedItem(BaseModel):
    """Schema for extracting a single item from the order-history LIST page.

    The list page essentially never shows per-item price/ASIN/quantity (see
    module docstring), so these fields default to safe placeholders instead
    of being required — the detail-page extraction below is what actually
    populates them. Kept mainly for the (rare) case a detail-page fetch is
    skipped or fails; see ``OrderHarvester._with_detail``.
    """

    model_config = {"populate_by_name": True}

    asin: str = Field(default="", description="Amazon Standard Identification Number")
    description: str = Field(default="", description="Item name/description")
    price_cents: int = Field(
        default=0,
        alias="priceCents",
        description="Price in cents (e.g., $49.77 = 4977)",
    )
    quantity: int = Field(default=1, description="Quantity ordered")

    @field_validator("asin", mode="before")
    @classmethod
    def _sanitize_asin(cls, value: object) -> str:
        return _clean_asin(value)

    @field_validator("price_cents", mode="after")
    @classmethod
    def _sanitize_price(cls, value: int) -> int:
        return _clamp_non_negative(value)

    @field_validator("quantity", mode="after")
    @classmethod
    def _sanitize_quantity(cls, value: int) -> int:
        return _clamp_positive_quantity(value)


class ExtractedOrder(BaseModel):
    """Schema for extracting a single Amazon order."""

    model_config = {"populate_by_name": True}

    order_id: str = Field(
        ..., alias="orderId", description="Order ID (e.g., 113-5524816-2451403)"
    )
    order_date: str = Field(
        ..., alias="orderDate", description="Order date in YYYY-MM-DD format"
    )
    order_total_cents: int = Field(
        ..., alias="orderTotalCents", description="Total in cents"
    )
    tax_cents: int = Field(
        default=0, alias="taxCents", description="Tax amount in cents"
    )
    shipping_cents: int = Field(
        default=0, alias="shippingCents", description="Shipping in cents"
    )
    items: list[ExtractedItem] = Field(default_factory=list, description="Order items")

    @field_validator("tax_cents", "shipping_cents", mode="after")
    @classmethod
    def _sanitize_non_negative(cls, value: int) -> int:
        return _clamp_non_negative(value)


class ExtractedDetailItem(BaseModel):
    """Schema for a single line item on an order's DETAIL page.

    Unlike the list page, the detail page is where Amazon actually renders
    price/ASIN/quantity — but extraction can still return noise for a field
    that genuinely isn't visible (a cancelled line, a gift-wrap fee with no
    ASIN). The validators below coerce that noise into safe values rather
    than letting it reach the splitter's arithmetic or the DB's
    ``(order_id, asin)`` identity.
    """

    model_config = {"populate_by_name": True}

    asin: str = Field(
        default="",
        description="ASIN; leave blank if the page shows none (e.g. digital goods)",
    )
    description: str = Field(default="", description="Item name/description")
    unit_price_cents: int = Field(
        default=0,
        alias="unitPriceCents",
        description="Price for ONE unit in cents (not price times quantity)",
    )
    quantity: int = Field(default=1, description="Quantity ordered")

    @field_validator("asin", mode="before")
    @classmethod
    def _sanitize_asin(cls, value: object) -> str:
        return _clean_asin(value)

    @field_validator("unit_price_cents", mode="after")
    @classmethod
    def _sanitize_price(cls, value: int) -> int:
        return _clamp_non_negative(value)

    @field_validator("quantity", mode="after")
    @classmethod
    def _sanitize_quantity(cls, value: int) -> int:
        return _clamp_positive_quantity(value)


class ExtractedOrderDetail(BaseModel):
    """Schema for extracting an order's DETAIL page: real items, tax, shipping."""

    model_config = {"populate_by_name": True}

    tax_cents: int = Field(
        default=0, alias="taxCents", description="Order-level tax in cents"
    )
    shipping_cents: int = Field(
        default=0, alias="shippingCents", description="Order-level shipping in cents"
    )
    items: list[ExtractedDetailItem] = Field(
        default_factory=list, description="Line items on this order"
    )

    @field_validator("tax_cents", "shipping_cents", mode="after")
    @classmethod
    def _sanitize_non_negative(cls, value: int) -> int:
        return _clamp_non_negative(value)


class ExtractedOrders(BaseModel):
    """Schema for extracting multiple orders from a page."""

    model_config = {"populate_by_name": True}

    orders: list[ExtractedOrder] = Field(
        default_factory=list, description="List of orders on current page"
    )
    has_next_page: bool = Field(
        default=False, alias="hasNextPage", description="Whether there are more orders"
    )


class OrdersPageVisible(BaseModel):
    """Sign-in probe: does the browser currently show the order list?"""

    visible: bool = Field(
        ...,
        description=(
            "True if this page shows an Amazon order history list (orders "
            "with dates and totals); false for a sign-in, OTP, or CAPTCHA page."
        ),
    )


def page_url(year_filter: int | None, page_num: int, base_url: str = ORDERS_URL) -> str:
    """URL for ``page_num`` (1-indexed) of the orders view.

    Why: Amazon's "Next" link is an SPA-style hyperlink that triggers a
    soft-navigation; under Stagehand the CDP target on the old frame is
    destroyed before Stagehand re-attaches, raising ``Page.evaluate: Target
    page, context or browser has been closed``. Direct navigation to the
    fully-qualified URL sidesteps that race.
    """
    params: list[str] = []
    if year_filter is not None:
        params.append(f"timeFilter=year-{year_filter}")
    if page_num > 1:
        params.append(f"startIndex={(page_num - 1) * _ORDERS_PER_PAGE}")
    if not params:
        return base_url
    return f"{base_url}?{'&'.join(params)}"


def detail_url(order_id: str) -> str:
    """URL for a single order's DETAIL page — the source of real item data."""
    return f"{DETAIL_URL_BASE}?orderID={order_id}"


def years_for_window(
    *,
    since: date | None,
    until: date | None,
    max_orders: int | None,
    today: date,
    floor_years: int = _DEFAULT_FLOOR_YEARS,
) -> list[int | None]:
    """Compute the year-filter URLs to visit for a given date window.

    Returns a most-recent-first list of years to iterate via Amazon's
    ``?timeFilter=year-{Y}`` URL parameter. A single ``None`` entry means
    "use Amazon's default view" (typically past 3 months) and is returned
    only when no constraint is provided at all.
    """
    if since is None and until is None and max_orders is None:
        return [None]

    upper = until.year if until is not None else today.year
    if upper > today.year:
        upper = today.year

    if since is not None:
        lower = since.year
    else:
        lower = today.year - floor_years

    if lower > upper:
        return []

    return list(range(upper, lower - 1, -1))


def final_url(response: Any) -> str | None:
    """Post-redirect URL of a ``navigate`` call, or ``None`` if unavailable.

    Stagehand returns a serialized Playwright ``Response``; the landed URL is
    the only part of that blob worth reading (it is how the sign-in redirect
    is detected without spending an LLM call).
    """
    result = getattr(getattr(response, "data", None), "result", None)
    if not isinstance(result, dict):
        return None
    page = result.get("page")
    if isinstance(page, dict) and isinstance(page.get("_currentUrl"), str):
        return page["_currentUrl"]
    http_response = result.get("response")
    if isinstance(http_response, dict) and isinstance(http_response.get("url"), str):
        return http_response["url"]
    return None


def is_signed_out(url: str | None) -> bool:
    """True when ``url`` is one of Amazon's authentication-portal URLs."""
    if url is None:
        return False
    return "/ap/signin" in url or "/ap/challenge" in url


async def navigate(session: Any, url: str) -> str | None:
    """Navigate to ``url``; return the URL actually landed on."""
    response = await session.navigate(url=url, options={"timeout": NAV_TIMEOUT_MS})
    landed = final_url(response)
    logger.debug("Navigated to {} (landed on {})", url, landed)
    return landed


async def orders_page_visible(session: Any) -> bool:
    """Whether the browser is currently showing the order-history list."""
    response = await session.extract(
        instruction="Is this page showing the Amazon order history list?",
        schema=OrdersPageVisible,
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    return bool(getattr(response.data.result, "visible", False))


async def wait_for_sign_in(
    session: Any, *, timeout_seconds: int, poll_seconds: int
) -> None:
    """Block until the order-history list appears, or raise ``TimeoutError``.

    The human is typing into the page, so re-navigating would wipe the form:
    ask the page what it is showing instead.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        await asyncio.sleep(poll_seconds)
        if await orders_page_visible(session):
            logger.success("Sign-in complete; orders page is visible")
            return
    raise TimeoutError(f"Timed out waiting for Amazon sign-in after {timeout_seconds}s")


class OrderHarvester:
    """Collects orders by walking the order-history view year by year.

    Holds the running list so a caller can persist what was gathered before a
    session died mid-scrape — a partial scrape is not a total loss.
    """

    def __init__(self) -> None:
        self.orders: list[ScrapedOrder] = []
        self._asin_reader: AsinReader | None = None

    async def harvest(
        self,
        session: Any,
        *,
        since: date | None,
        until: date | None,
        max_orders: int | None,
        fetch_item_details: bool = True,
        asin_reader: AsinReader | None = None,
    ) -> list[ScrapedOrder]:
        """Walk every year in the window and return the orders found.

        Args:
            fetch_item_details: When true (the default), each accepted order
                gets one extra navigation + extraction against its DETAIL
                page to get real per-item price/ASIN/quantity and
                order-level tax/shipping — the list page never carries them.
                ``max_orders`` still bounds how many orders (and therefore
                how many detail fetches) this performs.
            asin_reader: Optional capability to read real ASINs off the
                current page via Playwright/CDP (see
                ``backends/dom_asin_reader.py``). ``None`` (the default —
                every backend but the local one) means real ASINs are
                unavailable and every ASIN-less item gets the synthetic
                content-hash identity instead.
        """
        self._asin_reader = asin_reader
        year_filters = years_for_window(
            since=since, until=until, max_orders=max_orders, today=date.today()
        )
        logger.info(
            "Year filters resolved: {} (since={} until={})", year_filters, since, until
        )
        self.orders = []

        for year_filter in year_filters:
            if year_filter is not None:
                await navigate(session, page_url(year_filter, page_num=1))
                await asyncio.sleep(_PAGE_SETTLE_SECONDS)

            outcome = await self._harvest_view(
                session,
                since=since,
                until=until,
                max_orders=max_orders,
                year_filter=year_filter,
                fetch_item_details=fetch_item_details,
            )
            if outcome == "limit_hit":
                return self.orders[:max_orders]
            if outcome == "past_floor":
                logger.info(
                    "Year {} fully older than since={}; halting iteration",
                    year_filter,
                    since,
                )
                break

        logger.success("Finished scraping. Total orders: {}", len(self.orders))
        return self.orders

    async def _harvest_view(
        self,
        session: Any,
        *,
        since: date | None,
        until: date | None,
        max_orders: int | None,
        year_filter: int | None,
        fetch_item_details: bool,
    ) -> PageOutcome:
        """Extract every paginated page in the current view.

        Returns ``"limit_hit"`` when ``max_orders`` is reached, ``"past_floor"``
        when the current year produced ≥1 order all strictly older than
        ``since``, or ``"continue"`` to advance to the next year.
        """
        view_label = f"year={year_filter}" if year_filter is not None else "default"
        page_num = 0
        had_extractions = False
        all_older_than_since = True
        while True:
            page_num += 1
            logger.info("Extracting orders from page {} ({})", page_num, view_label)

            extracted = await self._extract_page(session)
            order_count = len(extracted.orders)
            logger.info(
                "Found {} orders on page {} ({})", order_count, page_num, view_label
            )
            # The list page's URL is what pagination needs; capture it before
            # any per-order detail fetch below navigates away from it.
            next_page_url = page_url(year_filter, page_num=page_num + 1)

            for order in extracted.orders:
                had_extractions = True
                try:
                    parsed_date = date.fromisoformat(order.order_date)
                except ValueError:
                    logger.warning(
                        "Skipping order {} with unparsable date '{}'",
                        order.order_id,
                        order.order_date,
                    )
                    continue

                if until is not None and parsed_date > until:
                    continue
                if since is not None and parsed_date < since:
                    continue
                if since is None or parsed_date >= since:
                    all_older_than_since = False

                scraped = _to_scraped_order(order)
                if fetch_item_details:
                    scraped = await self._with_detail(session, scraped)
                self.orders.append(scraped)

                if max_orders and len(self.orders) >= max_orders:
                    return "limit_hit"

            if order_count == 0 or not extracted.has_next_page:
                if since is not None and had_extractions and all_older_than_since:
                    return "past_floor"
                return "continue"

            await navigate(session, next_page_url)
            await asyncio.sleep(_PAGE_SETTLE_SECONDS)

    async def _extract_page(self, session: Any) -> ExtractedOrders:
        """Extract the orders on the page the session is currently showing."""
        response = await session.extract(
            instruction=_EXTRACT_INSTRUCTION,
            schema=ExtractedOrders,
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
        extracted = response.data.result
        if isinstance(extracted, ExtractedOrders):
            return extracted
        # The SDK hands back the raw payload when it fails to validate.
        return ExtractedOrders.model_validate(extracted)

    async def _with_detail(self, session: Any, order: ScrapedOrder) -> ScrapedOrder:
        """Replace ``order``'s list-page items with the detail page's real ones.

        Failure isolation: a detail-page fetch that errors (navigation
        timeout, an unparsable page, a Stagehand hiccup) must not sink an
        otherwise-good order — it logs and falls back to the list-page's
        placeholder items, which are already safe-by-construction (zero
        price / no items) rather than corrupting anything. This is one
        order's worth of degraded accuracy, not a lost scrape.
        """
        try:
            await navigate(session, detail_url(order.order_id))
            await asyncio.sleep(_PAGE_SETTLE_SECONDS)
            detail = await self._extract_detail(session)
        except Exception as exc:
            logger.warning(
                "Detail fetch failed for order {}; keeping list-page items: {}",
                order.order_id,
                exc,
            )
            return order

        if not detail.items:
            # Nothing extractable on the detail page (e.g. a fully cancelled
            # order) — keep whatever the list page had rather than dropping
            # to an empty item list the splitter would treat as "no items".
            logger.info(
                "Detail page for order {} yielded no items; keeping list-page items",
                order.order_id,
            )
            return order

        real_asins = await self._read_real_asins()
        # Only trust positional alignment between the DOM-read ASINs and the
        # LLM-extracted items when the counts agree — a mismatch means either
        # the DOM has non-item product links (e.g. a "buy again" widget) or
        # the extraction miscounted, and guessing at alignment risks writing
        # a real ASIN onto the wrong item, which is worse than no ASIN.
        matched_asins = real_asins if len(real_asins) == len(detail.items) else None
        if real_asins and matched_asins is None:
            logger.warning(
                "Order {}: {} product links but {} extracted items; skipping "
                "real-ASIN matching (counts must agree to trust alignment)",
                order.order_id,
                len(real_asins),
                len(detail.items),
            )

        items = [
            ScrapedItem(
                asin=_item_identity(
                    (matched_asins[idx] if matched_asins else "") or item.asin,
                    item.description,
                    item.unit_price_cents,
                    item.quantity,
                ),
                description=item.description,
                price_cents=item.unit_price_cents,
                quantity=item.quantity,
            )
            for idx, item in enumerate(detail.items)
        ]
        return order.model_copy(
            update={
                "items": items,
                "tax_cents": detail.tax_cents,
                "shipping_cents": detail.shipping_cents,
            }
        )

    async def _read_real_asins(self) -> list[str]:
        """Real ASINs off the currently-open page, or ``[]`` if unavailable.

        Never raises: a broken ``asin_reader`` must degrade this order to
        "no real ASINs" rather than sink the detail fetch already underway.
        """
        if self._asin_reader is None:
            return []
        try:
            return await self._asin_reader()
        except Exception as exc:
            logger.warning("asin_reader failed; continuing without real ASINs: {}", exc)
            return []

    async def _extract_detail(self, session: Any) -> ExtractedOrderDetail:
        """Extract the itemized detail on the page the session is showing."""
        response = await session.extract(
            instruction=_DETAIL_EXTRACT_INSTRUCTION,
            schema=ExtractedOrderDetail,
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
        extracted = response.data.result
        if isinstance(extracted, ExtractedOrderDetail):
            return extracted
        # The SDK hands back the raw payload when it fails to validate.
        return ExtractedOrderDetail.model_validate(extracted)


# Prefix marking a synthetic (non-Amazon) item identity. A real ASIN is
# always exactly 10 alphanumerics with no hyphen, so this prefix — and the
# resulting total length below — can never collide with one.
_SYNTHETIC_ASIN_PREFIX = "NOASIN-"
# Keeps the whole synthetic id within amazon_items.asin's String(20):
# len("NOASIN-") + 12 == 19.
_SYNTHETIC_ASIN_HASH_CHARS = 12


def _item_identity(asin: str, description: str, price_cents: int, quantity: int) -> str:
    """A stable identity for an item within one order, real ASIN or not.

    ``amazon_items`` is keyed on ``(order_id, asin)``; Amazon omits ASIN for
    some line items (digital goods, gift wrap, promotions). Those still need
    a synthetic identity so they don't all collapse onto the same
    ``(order_id, "")`` row on upsert — but that identity must be STABLE
    across re-scrapes, since sync is re-runnable and upserts key on it.

    Deriving it from the item's position in the extracted list (the
    previous approach) breaks that: extraction is LLM-driven, not a fixed
    DOM read, so nothing guarantees the same item lands at the same index on
    a re-scrape. A reordered re-scrape would then silently overwrite row N's
    price/description with a *different* item's data — real corruption, not
    just degraded accuracy. Hashing the item's own content instead means the
    same item always maps to the same synthetic id regardless of position.

    Trade-off: two ASIN-less items in the same order with byte-identical
    description/price/quantity collide onto one row — same as they always
    did, keyed on the empty string, before any synthetic identity existed.
    Rare in practice (Amazon typically folds identical items into one line
    via ``quantity``), and accepted in exchange for reorder-safety.
    """
    if asin:
        return asin
    digest = hashlib.sha256(
        f"{description}|{price_cents}|{quantity}".encode()
    ).hexdigest()
    return f"{_SYNTHETIC_ASIN_PREFIX}{digest[:_SYNTHETIC_ASIN_HASH_CHARS]}"


def _to_scraped_order(order: ExtractedOrder) -> ScrapedOrder:
    """Convert an extraction-shaped order into the scraper's domain type."""
    return ScrapedOrder(
        order_id=order.order_id,
        order_date=order.order_date,
        order_total_cents=order.order_total_cents,
        tax_cents=order.tax_cents,
        shipping_cents=order.shipping_cents,
        items=[
            ScrapedItem(
                asin=_item_identity(
                    item.asin, item.description, item.price_cents, item.quantity
                ),
                description=item.description,
                price_cents=item.price_cents,
                quantity=item.quantity,
            )
            for item in order.items
        ],
    )
