"""Amazon order scraper with multiple browser backend support."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel

if TYPE_CHECKING:
    from penny.adapters.db.facade import DB
    from penny.adapters.db.models import AmazonLoginProfileDB
    from penny.plugins.amazon.backends.base import AmazonScraperBackend

load_dotenv(override=False)


class ScrapedItem(BaseModel):
    """A single item scraped from an Amazon order."""

    asin: str
    description: str
    price_cents: int
    quantity: int


class ScrapedOrder(BaseModel):
    """A single Amazon order scraped from order history."""

    order_id: str
    order_date: str  # YYYY-MM-DD format
    order_total_cents: int
    tax_cents: int
    shipping_cents: int
    items: list[ScrapedItem]


class ScrapeResult(BaseModel):
    """Result of scraping Amazon orders."""

    orders: list[ScrapedOrder]


BackendType = Literal["playwriter", "stagehand", "stagehand-browserbase"]


def _max_or_none(*candidates: date | None) -> date | None:
    """Return the latest non-``None`` date among ``candidates`` (or ``None``)."""
    present = [c for c in candidates if c is not None]
    if not present:
        return None
    return max(present)


def _resolve_effective_since(
    user_since: date | None, db_floor: date | None
) -> date | None:
    """Pick the tighter of ``user_since`` and ``db_floor`` (``None`` = no bound)."""
    return _max_or_none(user_since, db_floor)


def _get_backend(
    backend: BackendType,
    *,
    context_id: str | None = None,
    profile_key: str | None = None,
) -> AmazonScraperBackend:
    """Get the backend instance for the specified type.

    Args:
        backend: Backend type to use.
        context_id: Optional Browserbase context ID for session persistence.
            Only applicable for "stagehand-browserbase" backend.
        profile_key: Login profile this backend serves. The local Stagehand
            backend keys its persistent Chrome user-data directory off it.

    Returns:
        Backend instance implementing AmazonScraperBackend protocol.

    Raises:
        ValueError: If backend type is not supported.
    """
    logger.info(
        "Selecting Amazon scraper backend: backend={} context_id_set={}",
        backend,
        context_id is not None,
    )
    if backend == "playwriter":
        from penny.plugins.amazon.backends.playwriter import PlaywriterBackend

        return PlaywriterBackend()
    elif backend == "stagehand":
        from penny.plugins.amazon.backends.stagehand_local import (
            StagehandLocalBackend,
        )

        return StagehandLocalBackend(profile_key=profile_key)
    elif backend == "stagehand-browserbase":
        from penny.plugins.amazon.backends.stagehand_browserbase import (
            StagehandBrowserbaseBackend,
        )

        return StagehandBrowserbaseBackend(context_id=context_id)
    else:
        raise ValueError(f"Unsupported backend: {backend}")


def _persist_orders(
    db: DB, orders: list[ScrapedOrder], profile_id: int
) -> tuple[int, int]:
    """Persist scraped orders to database.

    Args:
        db: Database facade for persisting data.
        orders: List of scraped orders to persist.
        profile_id: ID of the amazon_login_profiles row whose context produced
            this scrape; attributed onto every upserted order.

    Returns:
        Tuple of (orders_created, items_created) counts.
    """
    orders_created = 0
    items_created = 0

    for order in orders:
        db.upsert_amazon_order(
            order_id=order.order_id,
            order_date=date.fromisoformat(order.order_date),
            order_total_cents=order.order_total_cents,
            profile_id=profile_id,
            tax_cents=order.tax_cents,
            shipping_cents=order.shipping_cents,
        )
        orders_created += 1

        for item in order.items:
            db.upsert_amazon_item(
                order_id=order.order_id,
                asin=item.asin,
                description=item.description,
                price_cents=item.price_cents,
                quantity=item.quantity,
            )
            items_created += 1

    return orders_created, items_created


def _ensure_auth(
    db: DB,
    profile: AmazonLoginProfileDB,
    backend: BackendType,
) -> AmazonLoginProfileDB:
    """Ensure a profile is authenticated, creating a context and logging in if needed.

    Only Browserbase needs this phase: its auth lives in a server-side context
    that must exist before a session can attach to it. Every other backend owns
    its session state and signs in from within the scrape, so it is passed
    through untouched.

    For Browserbase the strategy is optimistic: if a context_id already exists,
    treat the profile as ready without launching a browser just to check. If no
    context_id exists, create one, run an interactive login flow, and persist
    the new context_id to the DB.

    Args:
        db: Database facade.
        profile: The login profile to authenticate.
        backend: Backend type to use.

    Returns:
        Updated profile (with context_id populated if newly created).

    Raises:
        Exception: If context creation or login fails.
    """
    log = logger.bind(profile_key=profile.profile_key)

    if backend != "stagehand-browserbase":
        # Only Browserbase keeps auth in a server-side context that has to
        # exist before a scrape. The other backends carry their own session
        # state (the local one, a per-profile Chrome user-data directory) and
        # drive any interactive sign-in from inside the scrape itself.
        log.info("Backend {} manages its own session; no auth phase", backend)
        return profile

    if profile.browserbase_context_id is not None:
        # Optimistic: treat existing context as valid. If the session is
        # actually expired, the scrape phase will surface the error.
        log.info("Profile has existing context_id; treating as ready (optimistic)")
        return profile

    # No context yet — create one and run interactive login.
    log.info(
        "No context found; creating new Browserbase context and starting login flow"
    )

    from penny.plugins.amazon.backends.stagehand_browserbase import (
        StagehandBrowserbaseBackend,
    )

    context_id = StagehandBrowserbaseBackend.create_context()
    profile = db.set_amazon_login_context_id(
        profile_key=profile.profile_key, context_id=context_id
    )
    log.info("Created and persisted Browserbase context_id")

    # Park on the sign-in page so the user can authenticate via Live View.
    StagehandBrowserbaseBackend(context_id=context_id, login_mode=True).login()

    db.record_amazon_login_auth_result(
        profile_key=profile.profile_key, status="success"
    )
    log.info("Login flow completed; profile is now ready")
    return profile


def _scrape_one_profile(
    db: DB,
    profile: AmazonLoginProfileDB,
    backend: BackendType,
    max_orders: int | None,
    since: date | None,
    until: date | None,
    is_unbounded_request: bool,
    fetch_item_details: bool,
) -> dict[str, Any]:
    """Run a scrape for a single profile and persist results.

    Args:
        db: Database facade for persisting scraped data.
        profile: The authenticated login profile to scrape.
        backend: Backend type to use.
        max_orders: Optional cap on orders scraped.
        since: Inclusive lower bound on order_date (effective floor — already
            includes the DB-derived floor; profile watermark is layered on top
            inside this function).
        until: Inclusive upper bound on order_date.
        is_unbounded_request: True when the user's original request had no
            ``since``/``until``/``max_orders`` constraints. Only an unbounded
            request that returns successfully advances the profile's
            ``history_complete_through`` watermark.
        fetch_item_details: Whether to fetch each order's detail page for
            real per-item price/ASIN/quantity and order-level tax/shipping.

    Returns:
        Per-profile result dict conforming to the result contract.
    """
    log = logger.bind(profile_key=profile.profile_key)
    final_since = _max_or_none(since, profile.history_complete_through)
    if final_since != since:
        log.info(
            "Profile watermark {} supersedes effective_since {}",
            profile.history_complete_through,
            since,
        )
    log.info(
        "Starting scrape for profile '{}' (since={} until={} max_orders={})",
        profile.display_name,
        final_since,
        until,
        max_orders,
    )

    backend_instance = _get_backend(
        backend,
        context_id=profile.browserbase_context_id,
        profile_key=profile.profile_key,
    )
    try:
        orders = backend_instance.scrape_order_history(
            since=final_since,
            until=until,
            max_orders=max_orders,
            fetch_item_details=fetch_item_details,
        )
    except Exception as exc:
        # Why: Browserbase sessions can die mid-scrape (timeouts, target loss);
        # extracted-but-unreturned rows live in the backend's `collected_orders`.
        # Persist them so a partial scrape isn't a total loss; upserts dedup.
        partial_orders: list[ScrapedOrder] = list(
            getattr(backend_instance, "collected_orders", [])
        )
        if partial_orders:
            orders_created, items_created = _persist_orders(
                db, partial_orders, profile.profile_id
            )
            log.warning(
                "Scrape raised after partial extraction: "
                "persisted {} orders, {} items; error={}",
                orders_created,
                items_created,
                exc,
            )
            return {
                "profile_key": profile.profile_key,
                "display_name": profile.display_name,
                "status": "partial",
                "orders_created": orders_created,
                "items_created": items_created,
                "message": (
                    f"Partial scrape: persisted {orders_created} orders, "
                    f"{items_created} items before error: {exc}"
                ),
            }
        log.warning("Scrape raised with no partial results: {}", exc)
        raise
    orders_created, items_created = _persist_orders(db, orders, profile.profile_id)

    if is_unbounded_request:
        watermark = date.today()
        db.set_amazon_profile_history_watermark(
            profile_id=profile.profile_id, through_date=watermark
        )
        log.info("Advanced history_complete_through watermark to {}", watermark)

    log.success(
        "Scrape complete: orders_created={} items_created={}",
        orders_created,
        items_created,
    )
    return {
        "profile_key": profile.profile_key,
        "display_name": profile.display_name,
        "status": "success",
        "orders_created": orders_created,
        "items_created": items_created,
        "message": f"Scraped {orders_created} orders, {items_created} items",
    }


def _scrape_profile_with_retry(
    db: DB,
    profile: AmazonLoginProfileDB,
    backend: BackendType,
    max_orders: int | None,
    since: date | None,
    until: date | None,
    is_unbounded_request: bool,
    fetch_item_details: bool,
) -> dict[str, Any]:
    """Scrape a single profile. Persists partial results on failure.

    Why no retry: Browserbase sessions are paid-per-minute and a fresh session
    starts iteration from page 1 — re-extracting rows already persisted in the
    failed attempt. Partial-persist + caller-driven re-run is cheaper.
    """
    log = logger.bind(profile_key=profile.profile_key)
    try:
        return _scrape_one_profile(
            db,
            profile,
            backend,
            max_orders,
            since,
            until,
            is_unbounded_request,
            fetch_item_details,
        )
    except Exception as exc:
        log.error("Scrape failed: {}", exc)
        return {
            "profile_key": profile.profile_key,
            "display_name": profile.display_name,
            "status": "error",
            "orders_created": 0,
            "items_created": 0,
            "message": str(exc),
        }


def _run_parallel_scrape(
    db: DB,
    ready_profiles: list[AmazonLoginProfileDB],
    backend: BackendType,
    max_orders: int | None,
    since: date | None,
    until: date | None,
    is_unbounded_request: bool,
    fetch_item_details: bool,
) -> list[dict[str, Any]]:
    """Run scraping for all ready profiles, one at a time on the main thread.

    Despite the name (preserved for plan/test continuity), this runs
    sequentially. The Stagehand client registers a SIGINT handler in its
    constructor via signal.signal(), which only works on the main thread of
    the main interpreter. A ThreadPoolExecutor would therefore raise
    "signal only works in main thread of the main interpreter" before the
    Browserbase session ever opens. True concurrency would require running
    backends as coroutines on a shared event loop on the main thread; that
    refactor is out of scope here.
    """
    return [
        _scrape_profile_with_retry(
            db,
            profile,
            backend,
            max_orders,
            since,
            until,
            is_unbounded_request,
            fetch_item_details,
        )
        for profile in ready_profiles
    ]


def _aggregate_results(
    profiles_total: int,
    profiles_ready: int,
    profile_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate per-profile results into a top-level summary.

    Args:
        profiles_total: Total number of enabled profiles.
        profiles_ready: Number of profiles that passed the auth phase.
        profile_results: List of per-profile result dicts.

    Returns:
        Top-level result dict conforming to the result contract.
    """
    succeeded = [r for r in profile_results if r["status"] == "success"]
    failed = [r for r in profile_results if r["status"] != "success"]
    total_orders = sum(r["orders_created"] for r in profile_results)
    total_items = sum(r["items_created"] for r in profile_results)

    if succeeded and len(succeeded) == len(profile_results):
        status = "success"
    elif succeeded:
        status = "partial"
    else:
        status = "error"

    return {
        "status": status,
        "orders_created": total_orders,
        "items_created": total_items,
        "profiles_total": profiles_total,
        "profiles_ready": profiles_ready,
        "profiles_succeeded": len(succeeded),
        "profiles_failed": len(failed),
        "message": (
            f"{len(succeeded)}/{profiles_ready} profiles succeeded, "
            f"{total_orders} orders, {total_items} items"
        ),
        "profile_results": profile_results,
    }


def _error_result(message: str, *, profiles_total: int = 0) -> dict[str, Any]:
    """Build a top-level error result with zero counts.

    Args:
        message: Human-readable error description.
        profiles_total: Total number of profiles considered (default 0).

    Returns:
        Top-level result dict with status "error".
    """
    return {
        "status": "error",
        "orders_created": 0,
        "items_created": 0,
        "profiles_total": profiles_total,
        "profiles_ready": 0,
        "profiles_succeeded": 0,
        "profiles_failed": 0,
        "message": message,
        "profile_results": [],
    }


def scrape_amazon_orders(
    db: DB,
    *,
    backend: BackendType = "stagehand-browserbase",
    since: date | None = None,
    until: date | None = None,
    max_orders: int | None = None,
    profile_key: str | None = None,
    fetch_item_details: bool = True,
) -> dict[str, Any]:
    """Scrape Amazon order history for all enabled login profiles.

    Runs a two-phase workflow:
    1. Sequential auth phase — validates (or establishes) authentication for
       each enabled profile, creating Browserbase contexts on first use.
    2. Sequential scrape phase — scrapes order history for each ready profile,
       with one retry per profile on failure.

    The orchestrator queries ``MIN(plaid_transactions.posted_at)`` and uses
    that as a lower bound: orders predating the earliest Plaid transaction
    cannot match against a real charge, so there is no value in scraping
    them. The DB floor is combined with ``since`` (the tighter of the two
    wins). When the DB has no transactions, only the user's ``since`` (if
    any) applies and a warning is logged.

    Args:
        db: Database facade for persisting scraped data and profile state.
        backend: Browser backend to use ("playwriter", "stagehand", or
            "stagehand-browserbase").
        since: Inclusive lower bound on ``order_date``. ``None`` means no
            user-supplied bound — the DB floor still applies.
        until: Inclusive upper bound on ``order_date``.
        max_orders: Optional maximum number of orders to scrape per profile.
        profile_key: If set, scrape only the profile with this key (must be
            enabled). When ``None``, scrape all enabled profiles.
        fetch_item_details: Whether to fetch each order's detail page for
            real per-item price/ASIN/quantity and order-level tax/shipping
            (P5). Defaults to on: the order-history list page never carries
            this data, so leaving it off means every item is a zero-priced
            placeholder and itemization degrades to an equal-cents split.
            Costs one extra navigation + LLM extraction per order — on a
            per-minute-billed Browserbase session, set it to ``False`` for a
            cheap/fast bulk backfill and re-run to itemize the important
            months. ``max_orders`` bounds the cost either way.

    Returns:
        Dictionary with top-level status and per-profile breakdown conforming
        to the result contract.
    """
    is_unbounded_request = since is None and until is None and max_orders is None
    db_floor = db.min_plaid_transaction_date()
    if db_floor is None:
        logger.warning("No plaid_transactions in DB; scraping with no DB-derived floor")
    effective_since = _resolve_effective_since(since, db_floor)
    if effective_since != since:
        logger.info("DB floor {} supersedes user --since {}", db_floor, since)

    profiles = db.list_amazon_login_profiles(enabled_only=True)
    if profile_key is not None:
        profiles = [p for p in profiles if p.profile_key == profile_key]
        if not profiles:
            return _error_result(
                f"No enabled Amazon login profile with key '{profile_key}'.",
                profiles_total=0,
            )
    if not profiles:
        return _error_result(
            "No enabled Amazon login profiles configured. "
            "Use add_amazon_login to add a profile.",
            profiles_total=0,
        )

    logger.info(
        "Starting Amazon scrape: backend={} profiles={} since={} until={} "
        "max_orders={} fetch_item_details={}",
        backend,
        len(profiles),
        effective_since,
        until,
        max_orders,
        fetch_item_details,
    )

    # Sequential auth phase
    ready_profiles: list[AmazonLoginProfileDB] = []
    for profile in profiles:
        log = logger.bind(profile_key=profile.profile_key)
        log.info("Auth phase: checking profile '{}'", profile.display_name)
        try:
            authenticated = _ensure_auth(db, profile, backend)
            ready_profiles.append(authenticated)
            log.info("Profile '{}' ready for scraping", profile.display_name)
        except Exception as exc:
            log.error("Auth failed for profile '{}': {}", profile.display_name, exc)
            db.record_amazon_login_auth_result(
                profile_key=profile.profile_key,
                status="failed",
                error=str(exc),
            )

    if not ready_profiles:
        return _error_result(
            "All profiles failed authentication.",
            profiles_total=len(profiles),
        )

    logger.info(
        "{}/{} profiles passed auth; starting scrape",
        len(ready_profiles),
        len(profiles),
    )

    profile_results = _run_parallel_scrape(
        db,
        ready_profiles,
        backend,
        max_orders,
        effective_since,
        until,
        is_unbounded_request,
        fetch_item_details,
    )

    return _aggregate_results(len(profiles), len(ready_profiles), profile_results)


def scrape_with_playwriter(db: DB) -> dict[str, Any]:
    """Execute Playwriter-powered scraping via OpenAI, then upsert results.

    This is a convenience wrapper around scrape_amazon_orders() using the
    Playwriter backend. For profile-based multi-login scraping, use
    scrape_amazon_orders() directly.

    Args:
        db: Database facade for persisting scraped data.

    Returns:
        Dictionary with status and summary of scraped data.
    """
    return scrape_amazon_orders(db, backend="playwriter")
