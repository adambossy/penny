"""Reads real Amazon ASINs directly from the DOM via Playwright over CDP.

Stagehand's `extract` only ever sees the accessibility tree it builds for
the model — no HTML, no link targets. A schema-less extract on a real order
detail page (46,824 chars) contained zero `amazon.com/` URLs and zero
ASIN-shaped strings; asking for "the product link URL" gets back Stagehand's
internal accessibility-tree node IDs (e.g. `'1-1826'`), never an href. No
prompt wording fixes this — the model literally cannot see what it was never
shown. So a real ASIN can only come from reading the page's own DOM, which
is what this module does.

It does so by *attaching* — never launching or closing — a second,
independent Playwright client to the SAME Chrome that Stagehand's local
server already started, over the Chrome DevTools Protocol. Stagehand keeps
sole ownership of that browser's lifecycle throughout; this client only
reads. See `LocalAsinReader.close()` for why disconnecting this client
cannot take Stagehand's browser down with it, and
`tests/plugins/amazon/test_dom_asin_reader.py::test_local_asin_reader_...`
for an offline proof against a real (throwaway) Chromium instance.

This is a capability of the LOCAL backend only. The Browserbase backend's
`sessions.start` response exposes a `cdp_url` too, but it points at a
browser in Browserbase's cloud infrastructure whose reachability from here
is untested and not guaranteed (network path, auth, firewalling all
differ from "same machine, OS-assigned loopback port") — attempting it
speculatively risks turning a currently-clean synthetic-identity fallback
into a flaky one. Browserbase keeps that fallback unconditionally; wiring a
Browserbase CDP attach is future work if a real need shows up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import re
import socket
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page

# Amazon renders a product link as .../dp/<ASIN>/... or
# .../gp/product/<ASIN>/... (both forms occur depending on the page and item
# type). An ASIN is always 10 alphanumerics; Amazon's own hrefs are already
# upper-case, but the pattern accepts either case and the caller normalizes.
_ASIN_HREF_PATTERN = re.compile(r"/(?:dp|gp/product)/([A-Za-z0-9]{10})(?:[/?]|$)")

# Selector for product links on an Amazon order-detail page. `a[href*=...]`
# matches the href attribute as authored (relative or absolute), which is
# then resolved to an absolute URL by reading `.href` (not `getAttribute`)
# in the page.eval below.
_PRODUCT_LINK_SELECTOR = "a[href*='/dp/'], a[href*='/gp/product/']"

# Order-detail URL substring used to pick the right tab when more than one
# is open (see `LocalAsinReader._current_page`); kept independent of
# order_history.DETAIL_URL_BASE to avoid this module depending on that one
# for a single string match.
_DETAIL_PAGE_URL_MARKER = "/gp/your-account/order-details"

CONNECT_TIMEOUT_MS = 15_000
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_SECONDS = 1.5


def free_local_port() -> int:
    """An OS-assigned free TCP port on localhost.

    Used to give Stagehand's local Chrome a known CDP port to listen on
    (`launch_options.port`) so this module can attach to it afterward. Racy
    in the general case (another process could grab the port before Chrome
    binds it), but "ask the kernel for a free port, then use it immediately"
    is the standard idiom, and the window here is a single local subprocess
    launch that follows right after.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _asin_from_href(href: str) -> str:
    """The ASIN in a single product-link href, or ``""`` if it doesn't match.

    Accepts both `/dp/<ASIN>` and `/gp/product/<ASIN>`. An href that matches
    neither shape (a non-product link the broad CSS selector still caught,
    e.g. a review-page link) yields ``""`` rather than raising.
    """
    match = _ASIN_HREF_PATTERN.search(href)
    return match.group(1).upper() if match else ""


# Minimum length (after normalization) the shorter side of a prefix match
# must reach before that prefix is trusted. Guards against a short, generic
# description ("Book", "Set of 2") spuriously prefix-matching an unrelated
# link's anchor text; a genuine Amazon product title — full or truncated —
# is always much longer than this in practice.
_MIN_PREFIX_MATCH_CHARS = 12


def _normalize_title(text: str) -> str:
    """Case- and whitespace-insensitive form of a product title, for matching."""
    return " ".join(text.split()).casefold()


def _titles_match(description: str, anchor_text: str) -> bool:
    """Whether an extracted item's description and a link's anchor text name
    the same product.

    Exact match after normalization is the common case: on a real order's
    detail page, the line items' anchor text is verbatim the same string the
    LLM extraction returns as `description` (see module docstring). When it
    isn't, the LLM's description is usually a truncated PREFIX of the full
    title (occasionally the reverse) — so a prefix match in either direction
    is also accepted, guarded by `_MIN_PREFIX_MATCH_CHARS` so a short,
    generic description can't spuriously match an unrelated link.
    """
    desc = _normalize_title(description)
    anchor = _normalize_title(anchor_text)
    if not desc or not anchor:
        return False
    if desc == anchor:
        return True
    shorter, longer = (desc, anchor) if len(desc) <= len(anchor) else (anchor, desc)
    return len(shorter) >= _MIN_PREFIX_MATCH_CHARS and longer.startswith(shorter)


def match_items_to_links(
    descriptions: Sequence[str], links: Sequence[tuple[str, str]]
) -> list[str]:
    """Match each item description to a DOM product link's ASIN, by content.

    Positional (count-aligned) matching is unsound here: a real order-detail
    page mixes actual line-item links with unrelated ones (recommendation
    widgets, "buy it again", a promo banner), so the number of product links
    on the page essentially never equals the number of extracted items — the
    prior implementation trusted alignment only when the counts happened to
    match, which was 0/8 real orders (0/24 items). Matching on title content
    instead works regardless of how many extra links share the page.

    Returns one entry per item in `descriptions`, in the same order: the
    matched link's ASIN, or `""` when no unused link's anchor text names
    that item (the caller falls back to its own content-hash identity for
    those). Each link is used for at most one item — a link is never
    assigned to two items on positional coincidence or an ambiguous match,
    and an unmatched link (promo, recommendation) is simply left unused.

    Two passes, so an ambiguous case can't steal a link an unambiguous case
    actually needs:
      1. Exact match (byte-identical after normalization) is resolved first.
      2. Prefix match (`_titles_match`) is resolved second, only among
         items/links neither pass 1 nor an earlier pass-2 iteration used.

    A link whose href doesn't parse to an ASIN (`_asin_from_href` returns
    `""` — a non-product link the broad CSS selector still admitted, or a
    product link Amazon rendered without the expected `/dp/`-shaped href) is
    never treated as a match; matching continues to the next candidate link.
    """
    matched: list[str] = ["" for _ in descriptions]
    used_links: set[int] = set()

    def _assign(is_match: Callable[[str, str], bool]) -> None:
        for item_idx, description in enumerate(descriptions):
            if matched[item_idx]:
                continue
            for link_idx, (href, anchor_text) in enumerate(links):
                if link_idx in used_links or not is_match(description, anchor_text):
                    continue
                asin = _asin_from_href(href)
                if not asin:
                    continue
                matched[item_idx] = asin
                used_links.add(link_idx)
                break

    _assign(lambda d, a: bool(d) and _normalize_title(d) == _normalize_title(a))
    _assign(_titles_match)
    return matched


class LocalAsinReader:
    """Read-only Playwright client attached to a local Stagehand browser.

    Construct with the CDP port Stagehand's Chrome was launched with, call
    `connect()` once per scrape session, then `read_current_page_links()`
    per order-detail page. Always `close()` when done (even on failure) —
    it only tears down this client's connection, never the browser.
    """

    def __init__(self, cdp_port: int) -> None:
        self._cdp_port = cdp_port
        self._playwright: Any = None
        self._browser: Browser | None = None

    async def connect(self) -> bool:
        """Attach to the browser Stagehand already started.

        Returns whether attaching worked. Failure (Chrome not listening yet,
        connection refused, `playwright` misconfigured) is never fatal to
        the scrape — callers treat a `False` return exactly like the
        Browserbase backend's permanent state: no real-ASIN capability this
        run, fall back to the synthetic item identity.
        """
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        last_exc: Exception | None = None
        for attempt in range(1, _CONNECT_ATTEMPTS + 1):
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self._cdp_port}",
                    timeout=CONNECT_TIMEOUT_MS,
                )
                logger.info(
                    "Playwright attached over CDP on port {} (attempt {})",
                    self._cdp_port,
                    attempt,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - any attach failure degrades gracefully
                last_exc = exc
                if attempt < _CONNECT_ATTEMPTS:
                    await asyncio.sleep(_CONNECT_RETRY_SECONDS)
        logger.warning(
            "Playwright could not attach over CDP on port {} after {} "
            "attempts; real ASINs are unavailable this run, falling back to "
            "the synthetic item identity: {}",
            self._cdp_port,
            _CONNECT_ATTEMPTS,
            last_exc,
        )
        await self.close()
        return False

    async def read_current_page_links(self) -> list[tuple[str, str]]:
        """(href, anchor text) for every product link on the current page.

        The href alone doesn't say WHICH extracted item a link belongs to —
        the page can carry more product links than the order has line items
        (recommendation widgets, "buy it again", a promo banner) — so the
        anchor text comes along for `match_items_to_links` to compare
        against each item's `description`. Anchor text is whitespace-
        collapsed and stripped here so callers never have to re-normalize
        raw DOM whitespace (nested `<span>`s, newlines) themselves.

        Returns `[]` on any failure rather than raising — a query error here
        must degrade an order to "no real ASINs" (the harvester then falls
        back to the LLM-extracted / synthetic identity), never sink the
        detail fetch that's already in progress.
        """
        browser = self._browser
        if browser is None:
            return []
        try:
            page = self._current_page(browser)
            if page is None:
                return []
            pairs = await page.eval_on_selector_all(
                _PRODUCT_LINK_SELECTOR,
                "els => els.map(el => [el.href, (el.textContent || '').trim()])",
            )
            return [(href, " ".join(text.split())) for href, text in pairs]
        except Exception as exc:  # noqa: BLE001 - degrade, never raise into the harvester
            logger.warning("Reading product links via Playwright/CDP failed: {}", exc)
            return []

    def _current_page(self, browser: Browser) -> Page | None:
        """The order-detail tab, if more than one page happens to be open.

        Stagehand drives a single tab through the whole scrape, so there is
        normally exactly one page across all contexts; when there's more
        than one, prefer whichever is actually showing an order-details URL
        rather than guessing by position.
        """
        pages = [page for ctx in browser.contexts for page in ctx.pages]
        if not pages:
            return None
        for page in pages:
            if _DETAIL_PAGE_URL_MARKER in page.url:
                return page
        return pages[-1]

    async def close(self) -> None:
        """Disconnect this client. Never closes Stagehand's browser.

        `connect_over_cdp` attaches to a browser this process did not
        launch; Playwright's documented behavior for that case is that
        closing the returned `Browser` only ends *this* client's CDP
        connection — the remote browser process (Stagehand's) is
        unaffected, the same way ending an SSH session doesn't reboot the
        server. `test_dom_asin_reader.py` proves this offline against a
        real (throwaway) Chromium: after `close()`, the browser that
        Chromium was launched from is still alive and usable.
        """
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Error disconnecting Playwright CDP client: {}", exc)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Error stopping Playwright: {}", exc)
            self._playwright = None
