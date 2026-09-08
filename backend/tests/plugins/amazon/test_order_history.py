"""Tests for the shared order-history page logic.

These helpers decide which Amazon URLs a scrape visits; both the local and
the Browserbase backend depend on them, so they are pinned here once. Also
covers the detail-page extraction schemas' safety coercion and the
per-order detail-fetch orchestration (``OrderHarvester._with_detail``) that
fixes the "LLM invents item data" corruption described in the bug this
branch closes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from penny.plugins.amazon.backends.order_history import (
    ExtractedDetailItem,
    ExtractedItem,
    ExtractedOrder,
    ExtractedOrderDetail,
    ExtractedOrders,
    OrderHarvester,
    _item_identity,
    _to_scraped_order,
    detail_url,
    page_url,
    years_for_window,
)
from penny.plugins.amazon.scraper import ScrapedOrder

TODAY = date(2026, 8, 31)


def test_page_url_uses_the_default_view_when_unconstrained() -> None:
    assert page_url(None, page_num=1) == "https://www.amazon.com/your-orders/orders"


def test_page_url_carries_the_year_filter_and_page_offset() -> None:
    assert page_url(2024, page_num=1).endswith("?timeFilter=year-2024")
    assert page_url(2024, page_num=3).endswith("?timeFilter=year-2024&startIndex=20")
    assert page_url(None, page_num=2).endswith("?startIndex=10")


def test_years_for_window_defaults_to_amazons_own_view() -> None:
    # No constraint at all: let Amazon show its default (recent) window.
    assert years_for_window(since=None, until=None, max_orders=None, today=TODAY) == [
        None
    ]


def test_years_for_window_walks_backwards_from_most_recent() -> None:
    years = years_for_window(
        since=date(2024, 3, 1), until=date(2026, 1, 5), max_orders=None, today=TODAY
    )
    assert years == [2026, 2025, 2024]


def test_years_for_window_never_looks_past_today() -> None:
    years = years_for_window(
        since=date(2026, 1, 1), until=date(2030, 1, 1), max_orders=None, today=TODAY
    )
    assert years == [2026]


def test_years_for_window_bounds_an_open_ended_lookback() -> None:
    # Why: a max_orders-only request has no floor; without the cap the scrape
    # would walk every year Amazon's dropdown offers.
    years = years_for_window(
        since=None, until=None, max_orders=5, today=TODAY, floor_years=3
    )
    assert years == [2026, 2025, 2024, 2023]


def test_years_for_window_is_empty_when_the_window_inverts() -> None:
    assert (
        years_for_window(
            since=date(2026, 1, 1), until=date(2024, 1, 1), max_orders=None, today=TODAY
        )
        == []
    )


def test_detail_url_carries_the_order_id() -> None:
    assert detail_url("113-5524816-2451403") == (
        "https://www.amazon.com/gp/your-account/order-details"
        "?orderID=113-5524816-2451403"
    )


# --- Extraction-schema safety coercion --------------------------------------
#
# Observed on real runs: the LLM fills fields the page doesn't show with
# invented "missing value" markers — 0 or -1 for numbers, "" or the literal
# string "null" for ASIN. Downstream, splitter.py multiplies price*quantity
# and itemization.py's proportional allocator raises on any negative input,
# so these have to be coerced to safe values at the extraction boundary
# rather than left to detonate later.


def test_extracted_detail_item_clamps_negative_price_to_zero() -> None:
    item = ExtractedDetailItem(unit_price_cents=-1)
    assert item.unit_price_cents == 0


def test_extracted_detail_item_clamps_non_positive_quantity_to_one() -> None:
    assert ExtractedDetailItem(quantity=-1).quantity == 1
    assert ExtractedDetailItem(quantity=0).quantity == 1


def test_extracted_detail_item_preserves_legitimate_values() -> None:
    item = ExtractedDetailItem(asin="B0123456789", unit_price_cents=4977, quantity=2)
    assert item.asin == "B0123456789"
    assert item.unit_price_cents == 4977
    assert item.quantity == 2


def test_extracted_detail_item_treats_filler_tokens_as_blank_asin() -> None:
    for filler in ("", "null", "NULL", "None", "n/a", "  unknown  "):
        assert ExtractedDetailItem(asin=filler).asin == ""


def test_extracted_detail_item_treats_non_string_asin_as_blank() -> None:
    # `extract` occasionally hands back a raw dict with the wrong JSON type.
    assert ExtractedDetailItem.model_validate({"asin": None}).asin == ""


def test_extracted_order_detail_clamps_negative_tax_and_shipping() -> None:
    detail = ExtractedOrderDetail(tax_cents=-1, shipping_cents=-5)
    assert detail.tax_cents == 0
    assert detail.shipping_cents == 0


def test_extracted_item_list_page_schema_defaults_are_safe() -> None:
    # The list page essentially never shows these fields; the defaults must
    # already satisfy the "non-negative, at-least-one-unit" contract on
    # their own, independent of the detail-page fetch ever running.
    item = ExtractedItem()
    assert item.asin == ""
    assert item.price_cents == 0
    assert item.quantity == 1


def test_extracted_order_clamps_negative_tax_and_shipping() -> None:
    order = ExtractedOrder(
        orderId="123-456",
        orderDate="2026-01-01",
        orderTotalCents=1000,
        taxCents=-1,
        shippingCents=-1,
    )
    assert order.tax_cents == 0
    assert order.shipping_cents == 0


# --- Item identity for ASIN-less line items ---------------------------------


def test_item_identity_passes_through_a_real_asin() -> None:
    assert _item_identity("B0123456789", "Widget", 4977, 1) == "B0123456789"


def test_item_identity_synthesizes_a_stable_id_when_asin_is_blank() -> None:
    # amazon_items is keyed on (order_id, asin); without a synthetic id every
    # blank-ASIN item in the same order would collide and only the last
    # upsert would survive.
    gift_wrap = _item_identity("", "Gift wrap", 299, 1)
    digital_credit = _item_identity("", "Digital credit", 500, 1)
    assert gift_wrap.startswith("NOASIN-")
    assert digital_credit.startswith("NOASIN-")
    assert gift_wrap != digital_credit


def test_item_identity_is_stable_across_calls_regardless_of_position() -> None:
    # The whole point of hashing content instead of list position: the same
    # item always maps to the same synthetic id, so a re-scrape that returns
    # items in a different order still upserts onto the same DB row.
    first_call = _item_identity("", "Gift wrap", 299, 1)
    second_call = _item_identity("", "Gift wrap", 299, 1)
    assert first_call == second_call


def test_item_identity_synthetic_id_fits_the_20_char_asin_column() -> None:
    identity = _item_identity("", "A very long product description " * 5, 999999, 3)
    assert len(identity) <= 20


def test_item_identity_synthetic_id_cannot_collide_with_a_real_asin() -> None:
    # Real ASINs are always exactly 10 alphanumerics with no hyphen; the
    # synthetic prefix and length make collision structurally impossible.
    identity = _item_identity("", "Gift wrap", 299, 1)
    assert "-" in identity
    assert len(identity) != 10


def test_to_scraped_order_gives_each_distinct_blank_asin_item_a_distinct_identity() -> (
    None
):
    order = ExtractedOrder(
        orderId="123-456",
        orderDate="2026-01-01",
        orderTotalCents=2000,
        items=[
            ExtractedItem(description="Gift wrap", priceCents=299),
            ExtractedItem(description="Digital credit", priceCents=500),
        ],
    )
    scraped = _to_scraped_order(order)
    asins = [item.asin for item in scraped.items]
    assert all(asin.startswith("NOASIN-") for asin in asins)
    assert len(set(asins)) == len(asins)


# --- OrderHarvester._with_detail: the per-order enrichment step ------------


class _FakeResponse:
    """Stand-in for the SDK's extract/navigate response envelope."""

    def __init__(self, result: Any) -> None:
        self.data = type("Data", (), {"result": result})()


class _FakeSession:
    """A session double that scripts one navigate + one extract call.

    No network or browser involved: ``navigate``/``extract`` just replay
    whatever this test configured, optionally raising to simulate a
    detail-page fetch failing.
    """

    def __init__(
        self,
        *,
        extract_result: Any = None,
        raise_on_navigate: Exception | None = None,
        raise_on_extract: Exception | None = None,
    ) -> None:
        self._extract_result = extract_result
        self._raise_on_navigate = raise_on_navigate
        self._raise_on_extract = raise_on_extract
        self.navigated_to: list[str] = []

    async def navigate(self, *, url: str, options: dict[str, Any]) -> _FakeResponse:
        self.navigated_to.append(url)
        if self._raise_on_navigate is not None:
            raise self._raise_on_navigate
        return _FakeResponse({"page": {"_currentUrl": url}})

    async def extract(self, *, instruction: str, schema: Any, timeout: float) -> Any:
        if self._raise_on_extract is not None:
            raise self._raise_on_extract
        return _FakeResponse(self._extract_result)


def _bare_order(order_id: str = "113-5524816-2451403") -> ScrapedOrder:
    return ScrapedOrder(
        order_id=order_id,
        order_date="2026-01-01",
        order_total_cents=5000,
        tax_cents=0,
        shipping_cents=0,
        items=[],
    )


async def test_with_detail_replaces_items_and_tax_shipping_on_success() -> None:
    detail = ExtractedOrderDetail(
        taxCents=200,
        shippingCents=100,
        items=[
            ExtractedDetailItem(
                asin="B0123456789",
                description="Widget",
                unitPriceCents=4700,
                quantity=1,
            )
        ],
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    enriched = await harvester._with_detail(session, _bare_order())

    assert enriched.tax_cents == 200
    assert enriched.shipping_cents == 100
    assert len(enriched.items) == 1
    assert enriched.items[0].asin == "B0123456789"
    assert enriched.items[0].price_cents == 4700
    assert session.navigated_to == [detail_url("113-5524816-2451403")]


async def test_with_detail_falls_back_to_list_page_items_on_navigate_failure() -> None:
    # Failure isolation: a dead detail-page fetch must not sink an
    # otherwise-good order — it should degrade to the list-page data rather
    # than raise and abandon the whole scrape.
    original = _bare_order()
    session = _FakeSession(raise_on_navigate=TimeoutError("nav timed out"))
    harvester = OrderHarvester()

    result = await harvester._with_detail(session, original)

    assert result == original


async def test_with_detail_falls_back_to_list_page_items_on_extract_failure() -> None:
    original = _bare_order()
    session = _FakeSession(raise_on_extract=RuntimeError("extraction failed"))
    harvester = OrderHarvester()

    result = await harvester._with_detail(session, original)

    assert result == original


async def test_with_detail_keeps_list_page_items_when_detail_page_has_none() -> None:
    # A fully-cancelled order's detail page may show zero line items; that
    # should not zero out an order that otherwise had list-page items.
    original = _bare_order()
    session = _FakeSession(extract_result=ExtractedOrderDetail(items=[]))
    harvester = OrderHarvester()

    result = await harvester._with_detail(session, original)

    assert result == original


async def test_with_detail_gives_distinct_identity_to_asin_less_items_with_different_content() -> (
    None
):
    detail = ExtractedOrderDetail(
        items=[
            ExtractedDetailItem(
                asin="null", description="Gift wrap", unitPriceCents=500
            ),
            ExtractedDetailItem(
                asin="", description="Digital credit", unitPriceCents=1000
            ),
        ]
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    enriched = await harvester._with_detail(session, _bare_order())

    asins = [item.asin for item in enriched.items]
    assert all(asin.startswith("NOASIN-") for asin in asins)
    assert len(set(asins)) == len(asins)


async def test_with_detail_collides_asin_less_items_with_identical_content() -> None:
    # Documented trade-off of content-hashed identity (see _item_identity's
    # docstring): two ASIN-less items with byte-identical description/price/
    # quantity collide onto the same synthetic id, same as they always did
    # when both were keyed on the empty string.
    detail = ExtractedOrderDetail(
        items=[
            ExtractedDetailItem(
                asin="null", description="Gift wrap", unitPriceCents=500
            ),
            ExtractedDetailItem(asin="", description="Gift wrap", unitPriceCents=500),
        ]
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    enriched = await harvester._with_detail(session, _bare_order())

    asins = [item.asin for item in enriched.items]
    assert asins[0] == asins[1]


# --- OrderHarvester._with_detail: real ASINs matched by DOM link content ---


async def test_with_detail_matches_real_asins_by_description_not_position() -> None:
    # The whole point of the capability: a real ASIN from the DOM overrides
    # whatever (unreliable) value the LLM extraction put in item.asin — and
    # it does so by matching title content, not by list position/count.
    detail = ExtractedOrderDetail(
        items=[
            ExtractedDetailItem(
                asin="", description="Widget", unitPriceCents=4700, quantity=1
            ),
            ExtractedDetailItem(
                asin="", description="Gadget", unitPriceCents=1200, quantity=2
            ),
        ]
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    async def fake_link_reader() -> list[tuple[str, str]]:
        return [
            ("https://www.amazon.com/dp/B012345678", "Widget"),
            ("https://www.amazon.com/dp/B098765432", "Gadget"),
        ]

    harvester._link_reader = fake_link_reader
    enriched = await harvester._with_detail(session, _bare_order())

    assert [item.asin for item in enriched.items] == ["B012345678", "B098765432"]


async def test_with_detail_ignores_extra_unrelated_links_on_the_page() -> None:
    # A real detail page mixes actual line-item links with unrelated ones
    # (recommendations, "buy it again", promo banners) — the page having
    # more product links than the order has items must not block matching
    # the one that actually names this item.
    detail = ExtractedOrderDetail(
        items=[
            ExtractedDetailItem(
                asin="", description="Widget", unitPriceCents=4700, quantity=1
            ),
        ]
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    async def fake_link_reader() -> list[tuple[str, str]]:
        return [
            ("https://www.amazon.com/dp/B012345678", "Widget"),
            ("https://www.amazon.com/dp/B0DVBL912R", "Amazon Business Card"),
            ("https://www.amazon.com/dp/B055555555", "Frequently bought together"),
        ]

    harvester._link_reader = fake_link_reader
    enriched = await harvester._with_detail(session, _bare_order())

    assert enriched.items[0].asin == "B012345678"


async def test_with_detail_leaves_asin_synthetic_when_no_link_names_the_item() -> None:
    # The DOM has product links, but none of their anchor text names this
    # item — a mismatch, not an alignment failure — so it keeps the
    # content-hash fallback rather than guessing.
    detail = ExtractedOrderDetail(
        items=[
            ExtractedDetailItem(
                asin="", description="Widget", unitPriceCents=4700, quantity=1
            ),
        ]
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    async def fake_link_reader() -> list[tuple[str, str]]:
        return [("https://www.amazon.com/dp/B098765432", "Something unrelated")]

    harvester._link_reader = fake_link_reader
    enriched = await harvester._with_detail(session, _bare_order())

    assert enriched.items[0].asin.startswith("NOASIN-")


async def test_with_detail_falls_back_when_link_reader_raises() -> None:
    detail = ExtractedOrderDetail(
        items=[
            ExtractedDetailItem(
                asin="", description="Widget", unitPriceCents=4700, quantity=1
            ),
        ]
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    async def broken_link_reader() -> list[tuple[str, str]]:
        raise RuntimeError("CDP connection dropped")

    harvester._link_reader = broken_link_reader
    enriched = await harvester._with_detail(session, _bare_order())

    assert enriched.items[0].asin.startswith("NOASIN-")


async def test_with_detail_leaves_llm_asin_untouched_when_no_reader_configured() -> (
    None
):
    # Backends without the capability (Browserbase, or a failed local
    # attach) pass link_reader=None to harvest(); _with_detail must behave
    # exactly as it did before this capability existed.
    detail = ExtractedOrderDetail(
        items=[
            ExtractedDetailItem(
                asin="B0123456789", description="Widget", unitPriceCents=4700
            ),
        ]
    )
    session = _FakeSession(extract_result=detail)
    harvester = OrderHarvester()

    enriched = await harvester._with_detail(session, _bare_order())

    assert enriched.items[0].asin == "B0123456789"


async def test_harvest_threads_link_reader_into_with_detail(monkeypatch: Any) -> None:
    """`harvest()` is the only place `link_reader` is normally set; pin the wiring."""
    harvester = OrderHarvester()
    captured: list[Any] = []

    async def fake_with_detail(session: Any, order: ScrapedOrder) -> ScrapedOrder:
        captured.append(harvester._link_reader)
        return order

    monkeypatch.setattr(harvester, "_with_detail", fake_with_detail)

    async def fake_extract_page(session: Any) -> ExtractedOrders:
        return ExtractedOrders(
            orders=[
                ExtractedOrder(
                    orderId="113-5524816-2451403",
                    orderDate="2026-01-01",
                    orderTotalCents=1000,
                )
            ],
            hasNextPage=False,
        )

    monkeypatch.setattr(harvester, "_extract_page", fake_extract_page)

    async def reader() -> list[tuple[str, str]]:
        return []

    await harvester.harvest(
        _FakeSession(), since=None, until=None, max_orders=None, link_reader=reader
    )

    assert captured == [reader]
