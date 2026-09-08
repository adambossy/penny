"""Tests for the Playwright/CDP real-ASIN reader.

Two layers:
  - Pure ``_asin_from_href`` logic — no browser involved, always runs.
  - An offline end-to-end proof against a real (throwaway) Chromium: attach
    over CDP, read ASINs from a local HTML fixture, and confirm closing the
    reader does not take down the browser it attached to. This needs
    Chromium binaries (`uv run playwright install chromium`); it skips
    cleanly when they aren't present rather than failing the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from penny.plugins.amazon.backends.dom_asin_reader import (
    LocalAsinReader,
    _asin_from_href,
    free_local_port,
    match_items_to_links,
)

# --- _asin_from_href: pure logic, various href shapes -----------------------


def test_parses_asin_from_a_dp_style_href() -> None:
    href = (
        "https://www.amazon.com/Some-Widget/dp/B01ABCDEF2/ref=cm_cr_arp_d_product_top"
    )
    assert _asin_from_href(href) == "B01ABCDEF2"


def test_parses_asin_from_a_gp_product_style_href() -> None:
    href = "https://www.amazon.com/gp/product/B00X4WHP5E/ref=ppx_yo_dt_b"
    assert _asin_from_href(href) == "B00X4WHP5E"


def test_parses_asin_from_a_bare_dp_href_with_no_trailing_slash() -> None:
    assert _asin_from_href("https://www.amazon.com/dp/B01ABCDEF2") == "B01ABCDEF2"


def test_parses_asin_from_a_dp_href_with_a_trailing_slash() -> None:
    assert _asin_from_href("https://www.amazon.com/dp/B01ABCDEF2/") == "B01ABCDEF2"


def test_parses_asin_from_a_dp_href_with_a_query_string() -> None:
    assert (
        _asin_from_href("https://www.amazon.com/dp/B01ABCDEF2?th=1&psc=1")
        == "B01ABCDEF2"
    )


def test_parses_asin_from_a_relative_href() -> None:
    assert _asin_from_href("/dp/B01ABCDEF2/ref=od_ep") == "B01ABCDEF2"


def test_normalizes_lowercase_asin_characters_to_uppercase() -> None:
    assert _asin_from_href("https://www.amazon.com/dp/b01abcdef2") == "B01ABCDEF2"


def test_ignores_non_product_hrefs() -> None:
    for href in (
        "https://www.amazon.com/gp/help/customer/display.html",
        "https://www.amazon.com/review/R1234567890",
        "https://www.amazon.com/gp/css/order-history",
    ):
        assert _asin_from_href(href) == ""


def test_free_local_port_returns_a_usable_port_number() -> None:
    port = free_local_port()
    assert 0 < port < 65536


# --- match_items_to_links: content matching, not positional alignment ------
#
# Ground truth (verified live against a real order, see the branch's task
# brief): a real detail page mixes actual line-item links with unrelated
# ones, so counts never line up — matching has to go by title content.

_BINDER_HREF = "https://www.amazon.com/dp/B00006IEM6/ref=od_ep"
_BINDER_TITLE = 'Avery Showcase 3 Ring Binder, 1.5" Slant Rings, 1 White Binder'
_BIB_HREF = "https://www.amazon.com/dp/B0FN4WCVBL/ref=od_ep"
_BIB_TITLE = "BebeBiu Long Sleeve Baby Bib, Waterproof Fabric, Full Coverage"
_PROMO_HREF = "https://www.amazon.com/dp/B0DVBL912R/ref=promo"
_PROMO_TITLE = "Amazon Business Card"


def test_match_items_to_links_matches_on_exact_title() -> None:
    matched = match_items_to_links(
        [_BINDER_TITLE, _BIB_TITLE],
        [(_BINDER_HREF, _BINDER_TITLE), (_BIB_HREF, _BIB_TITLE)],
    )
    assert matched == ["B00006IEM6", "B0FN4WCVBL"]


def test_match_items_to_links_ignores_case_and_whitespace_differences() -> None:
    matched = match_items_to_links(
        ['  avery showcase 3 ring   binder, 1.5" slant rings, 1 white binder  '],
        [(_BINDER_HREF, _BINDER_TITLE)],
    )
    assert matched == ["B00006IEM6"]


def test_match_items_to_links_matches_a_truncated_llm_description_as_a_prefix() -> None:
    # The LLM's description is sometimes a truncated prefix of the full title.
    truncated = 'Avery Showcase 3 Ring Binder, 1.5" Slant Rings'
    matched = match_items_to_links([truncated], [(_BINDER_HREF, _BINDER_TITLE)])
    assert matched == ["B00006IEM6"]


def test_match_items_to_links_matches_when_the_anchor_text_is_the_shorter_one() -> None:
    # Less common, but the truncation could in principle run the other way.
    truncated_anchor = _BINDER_TITLE[:20]
    matched = match_items_to_links([_BINDER_TITLE], [(_BINDER_HREF, truncated_anchor)])
    assert matched == ["B00006IEM6"]


def test_match_items_to_links_rejects_a_short_generic_prefix() -> None:
    # A short, generic description must not spuriously prefix-match an
    # unrelated link just because it happens to be a textual prefix.
    matched = match_items_to_links(["Binder"], [(_BINDER_HREF, _BINDER_TITLE)])
    assert matched == [""]


def test_match_items_to_links_leaves_an_unmatched_item_blank() -> None:
    matched = match_items_to_links(
        [_BINDER_TITLE, "Some item with no matching link at all here"],
        [(_BINDER_HREF, _BINDER_TITLE)],
    )
    assert matched == ["B00006IEM6", ""]


def test_match_items_to_links_ignores_an_unmatched_promo_link() -> None:
    # The 19th link on the real test order's detail page (a "Get the Amazon
    # Business Card" promo) matches no line item and must not be forced onto
    # one just because it's on the page.
    matched = match_items_to_links(
        [_BINDER_TITLE],
        [(_PROMO_HREF, _PROMO_TITLE), (_BINDER_HREF, _BINDER_TITLE)],
    )
    assert matched == ["B00006IEM6"]


def test_match_items_to_links_never_assigns_one_link_to_two_items() -> None:
    # Two items with byte-identical descriptions but only one candidate link:
    # the link is consumed by the first match, the second item is left blank
    # rather than the same ASIN being duplicated onto both.
    matched = match_items_to_links(
        [_BINDER_TITLE, _BINDER_TITLE], [(_BINDER_HREF, _BINDER_TITLE)]
    )
    assert matched == ["B00006IEM6", ""]


def test_match_items_to_links_skips_a_link_whose_href_has_no_asin() -> None:
    # A title-matching link that isn't shaped like a product href (broad CSS
    # selector let it through, but it's not `/dp/<ASIN>` or
    # `/gp/product/<ASIN>`) contributes no ASIN; matching keeps looking.
    unparseable_href = "https://www.amazon.com/dp//ref=od_ep"
    matched = match_items_to_links(
        [_BINDER_TITLE],
        [(unparseable_href, _BINDER_TITLE), (_BINDER_HREF, _BINDER_TITLE)],
    )
    assert matched == ["B00006IEM6"]


def test_match_items_to_links_with_no_links_at_all() -> None:
    assert match_items_to_links([_BINDER_TITLE], []) == [""]


def test_match_items_to_links_with_no_items_at_all() -> None:
    assert match_items_to_links([], [(_BINDER_HREF, _BINDER_TITLE)]) == []


def test_match_items_to_links_treats_a_blank_description_as_unmatchable() -> None:
    matched = match_items_to_links(["", _BIB_TITLE], [(_BIB_HREF, _BIB_TITLE)])
    assert matched == ["", "B0FN4WCVBL"]


# --- Offline end-to-end proof: attach over CDP, read the DOM, disconnect ---
#
# Simulates production exactly: an "owner" browser launched the way
# Stagehand's local server launches Chrome (with a fixed --remote-debugging
# CDP port), and a completely separate LocalAsinReader attaches to it
# read-only, the way stagehand_local.py's scrape does. Proves three things
# without needing Amazon: (1) attaching over CDP to an already-running
# browser works, (2) hrefs read off a real DOM parse to the right ASINs,
# (3) closing the reader's connection does NOT close the browser it attached
# to — the load-bearing safety property for "Stagehand must keep working
# exactly as now".

FIXTURE_HTML = """
<!doctype html>
<html>
<body>
  <h1>Your Orders</h1>
  <div class="order-item">
    <a href="/gp/product/B00X4WHP5E/ref=ppx_yo_dt_b_product_details">
      A Gadget
    </a>
  </div>
  <div class="order-item">
    <a href="https://www.amazon.com/Some-Widget/dp/B01ABCDEF2/ref=cm_cr_arp_d_product_top">
      A Widget
    </a>
  </div>
  <div class="order-item">
    <!-- Duplicate link to the same product (title + thumbnail), must dedupe -->
    <a href="/dp/B01ABCDEF2/ref=od_ep">A Widget (image link)</a>
  </div>
  <a href="/gp/css/order-history">Not a product link</a>
</body>
</html>
"""


async def test_local_asin_reader_reads_real_asins_from_a_live_chromium_dom(
    tmp_path: Path,
) -> None:
    from playwright.async_api import async_playwright

    fixture_path = tmp_path / "order_detail.html"
    fixture_path.write_text(FIXTURE_HTML)

    port = free_local_port()
    owner_playwright = await async_playwright().start()
    try:
        owner_browser = await owner_playwright.chromium.launch(
            headless=True, args=[f"--remote-debugging-port={port}"]
        )
    except Exception as exc:
        await owner_playwright.stop()
        pytest.skip(
            f"Chromium not installed (run `playwright install chromium`): {exc}"
        )
        return

    try:
        owner_page = await owner_browser.new_page()
        await owner_page.goto(fixture_path.as_uri())

        # This mirrors exactly what stagehand_local.py does: attach a second,
        # independent Playwright client to the browser that is already
        # running, over the same CDP port.
        reader = LocalAsinReader(port)
        attached = await reader.connect()
        assert attached, "LocalAsinReader failed to attach to the owner browser"

        links = await reader.read_current_page_links()
        assert [text for _, text in links] == [
            "A Gadget",
            "A Widget",
            "A Widget (image link)",
        ]
        assert [_asin_from_href(href) for href, _ in links] == [
            "B00X4WHP5E",
            "B01ABCDEF2",
            "B01ABCDEF2",  # the image link points at the same product
        ]

        await reader.close()

        # The load-bearing safety property: disconnecting the reader must
        # not have taken the owner's browser down with it.
        assert owner_browser.is_connected()
        assert await owner_page.title() == ""
    finally:
        await owner_browser.close()
        await owner_playwright.stop()


async def test_local_asin_reader_connect_fails_gracefully_on_a_closed_port() -> None:
    """No Chrome listening on the port at all: connect() returns False, never raises.

    Exercises the retry loop's give-up path against a port nothing is
    listening on (a few seconds of real retries — deliberately not mocked,
    to prove `connect()` truly never raises out of this path).
    """
    port = free_local_port()  # guaranteed nothing is listening on it
    reader = LocalAsinReader(port)

    attached = await reader.connect()

    assert attached is False
