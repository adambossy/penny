"""Tests for the Playwright/CDP real-ASIN reader.

Two layers:
  - Pure ``parse_asins_from_hrefs`` logic — no browser involved, always runs.
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
    free_local_port,
    parse_asins_from_hrefs,
)

# --- parse_asins_from_hrefs: pure logic, various href shapes ----------------


def test_parses_asin_from_a_dp_style_href() -> None:
    hrefs = [
        "https://www.amazon.com/Some-Widget/dp/B01ABCDEF2/ref=cm_cr_arp_d_product_top"
    ]
    assert parse_asins_from_hrefs(hrefs) == ["B01ABCDEF2"]


def test_parses_asin_from_a_gp_product_style_href() -> None:
    hrefs = ["https://www.amazon.com/gp/product/B00X4WHP5E/ref=ppx_yo_dt_b"]
    assert parse_asins_from_hrefs(hrefs) == ["B00X4WHP5E"]


def test_parses_asin_from_a_bare_dp_href_with_no_trailing_slash() -> None:
    assert parse_asins_from_hrefs(["https://www.amazon.com/dp/B01ABCDEF2"]) == [
        "B01ABCDEF2"
    ]


def test_parses_asin_from_a_dp_href_with_a_trailing_slash() -> None:
    assert parse_asins_from_hrefs(["https://www.amazon.com/dp/B01ABCDEF2/"]) == [
        "B01ABCDEF2"
    ]


def test_parses_asin_from_a_dp_href_with_a_query_string() -> None:
    assert parse_asins_from_hrefs(
        ["https://www.amazon.com/dp/B01ABCDEF2?th=1&psc=1"]
    ) == ["B01ABCDEF2"]


def test_parses_asin_from_a_relative_href() -> None:
    assert parse_asins_from_hrefs(["/dp/B01ABCDEF2/ref=od_ep"]) == ["B01ABCDEF2"]


def test_normalizes_lowercase_asin_characters_to_uppercase() -> None:
    assert parse_asins_from_hrefs(["https://www.amazon.com/dp/b01abcdef2"]) == [
        "B01ABCDEF2"
    ]


def test_ignores_non_product_hrefs() -> None:
    hrefs = [
        "https://www.amazon.com/gp/help/customer/display.html",
        "https://www.amazon.com/review/R1234567890",
        "https://www.amazon.com/gp/css/order-history",
    ]
    assert parse_asins_from_hrefs(hrefs) == []


def test_dedupes_the_same_asin_seen_twice_preserving_first_seen_order() -> None:
    hrefs = [
        "https://www.amazon.com/dp/B01ABCDEF2/ref=title",
        "https://www.amazon.com/dp/B00X4WHP5E/ref=title",
        "https://www.amazon.com/dp/B01ABCDEF2/ref=image",  # same product, image link
    ]
    assert parse_asins_from_hrefs(hrefs) == ["B01ABCDEF2", "B00X4WHP5E"]


def test_empty_input_yields_empty_output() -> None:
    assert parse_asins_from_hrefs([]) == []


def test_free_local_port_returns_a_usable_port_number() -> None:
    port = free_local_port()
    assert 0 < port < 65536


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

        asins = await reader.read_current_page_asins()
        assert asins == ["B00X4WHP5E", "B01ABCDEF2"]

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
