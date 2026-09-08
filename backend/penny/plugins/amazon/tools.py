"""Amazon plugin @tool surface.

Six tools — four profile-CRUD + two action tools (scrape, remutate). The
heavy lifting (scraper, backends, mutation/match/split logic) lives in
sibling modules and in ``penny.adapters.amazon``. This file is the agent's
view of the plugin.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Literal, cast

from agent_harness import tool

from penny.db import get_db

from .remutate import remutate_amazon_orders as _remutate_amazon
from .scraper import scrape_amazon_orders as _scrape_amazon

BackendType = Literal["stagehand", "stagehand-browserbase", "playwriter"]


# --- Login profile CRUD ----------------------------------------------------


@tool
async def list_amazon_logins() -> dict[str, Any]:
    """List every configured Amazon login profile."""

    def _run() -> dict[str, Any]:
        profiles = get_db().list_amazon_login_profiles()
        return {
            "profiles": [
                {
                    "profile_key": p.profile_key,
                    "display_name": p.display_name,
                    "enabled": p.enabled,
                    "sort_order": p.sort_order,
                    "has_context": p.browserbase_context_id is not None,
                    "last_auth_status": p.last_auth_status,
                    "last_auth_at": p.last_auth_at.isoformat()
                    if p.last_auth_at
                    else None,
                }
                for p in profiles
            ]
        }

    return await asyncio.to_thread(_run)


@tool
async def add_amazon_login(
    profile_key: str,
    display_name: str,
    enabled: bool = True,
    sort_order: int = 0,
) -> dict[str, Any]:
    """Add a new Amazon login profile."""

    def _run() -> dict[str, Any]:
        try:
            profile = get_db().create_amazon_login_profile(
                profile_key=profile_key,
                display_name=display_name,
                enabled=enabled,
                sort_order=sort_order,
            )
            return {
                "status": "success",
                "profile_key": profile.profile_key,
                "display_name": profile.display_name,
                "message": f"Created profile '{profile_key}'",
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    return await asyncio.to_thread(_run)


@tool
async def update_amazon_login(
    profile_key: str,
    display_name: str | None = None,
    enabled: bool | None = None,
    sort_order: int | None = None,
) -> dict[str, Any]:
    """Update an Amazon login profile (at least one field is required)."""

    if display_name is None and enabled is None and sort_order is None:
        return {
            "status": "error",
            "message": "At least one of display_name, enabled, sort_order must be provided",
        }

    def _run() -> dict[str, Any]:
        try:
            profile = get_db().update_amazon_login_profile(
                profile_key=profile_key,
                display_name=display_name,
                enabled=enabled,
                sort_order=sort_order,
            )
            return {
                "status": "success",
                "profile_key": profile.profile_key,
                "message": f"Updated profile '{profile_key}'",
            }
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    return await asyncio.to_thread(_run)


@tool
async def remove_amazon_login(profile_key: str) -> dict[str, Any]:
    """Delete an Amazon login profile."""

    def _run() -> dict[str, Any]:
        try:
            get_db().delete_amazon_login_profile(profile_key=profile_key)
            return {"status": "success", "message": f"Removed profile '{profile_key}'"}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    return await asyncio.to_thread(_run)


@tool
async def enable_amazon_login(profile_key: str) -> dict[str, Any]:
    """Enable an Amazon login profile so it's included in scrapes."""

    def _run() -> dict[str, Any]:
        try:
            get_db().update_amazon_login_profile(profile_key=profile_key, enabled=True)
            return {"status": "success", "message": f"Enabled profile '{profile_key}'"}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    return await asyncio.to_thread(_run)


@tool
async def disable_amazon_login(profile_key: str) -> dict[str, Any]:
    """Disable an Amazon login profile so scrapes skip it."""

    def _run() -> dict[str, Any]:
        try:
            get_db().update_amazon_login_profile(profile_key=profile_key, enabled=False)
            return {"status": "success", "message": f"Disabled profile '{profile_key}'"}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    return await asyncio.to_thread(_run)


@tool
async def clear_amazon_login_context(profile_key: str) -> dict[str, Any]:
    """Clear the stored Browserbase context for an Amazon login profile.

    Forces a fresh login on the next scrape — use when a profile's session
    has gone stale or authentication is failing.
    """

    def _run() -> dict[str, Any]:
        try:
            get_db().set_amazon_login_context_id(
                profile_key=profile_key, context_id=None
            )
            return {
                "status": "success",
                "message": f"Cleared context for profile '{profile_key}'",
            }
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    return await asyncio.to_thread(_run)


# --- Action tools ----------------------------------------------------------


@tool
async def scrape_amazon_orders(
    since: str | None = None,
    until: str | None = None,
    max_orders: int = 10,
    backend: str = "stagehand-browserbase",
    profile_key: str | None = None,
    fetch_item_details: bool = True,
) -> dict[str, Any]:
    """Scrape Amazon order history for every enabled profile.

    Args:
        since: Inclusive ISO ``YYYY-MM-DD`` lower bound on order date.
        until: Inclusive ISO ``YYYY-MM-DD`` upper bound.
        max_orders: Maximum orders per profile (default 10).
        backend: ``"stagehand"`` (local), ``"stagehand-browserbase"``
            (cloud), or ``"playwriter"``.
        profile_key: If set, scrape only this profile.
        fetch_item_details: Fetch each order's detail page for real
            per-item price/ASIN/quantity and order-level tax/shipping
            (default True — the order-history list page never carries this
            data). Costs one extra page load + LLM call per order; set to
            ``False`` only for a cheap/fast bulk backfill where per-item
            accuracy doesn't matter yet. ``max_orders`` still caps the work
            either way.
    """
    if backend not in ("stagehand", "stagehand-browserbase", "playwriter"):
        return {
            "status": "error",
            "message": f"Invalid backend: {backend}",
            "orders_created": 0,
            "items_created": 0,
            "profiles_total": 0,
            "profiles_ready": 0,
            "profiles_succeeded": 0,
            "profiles_failed": 0,
            "profile_results": [],
        }

    try:
        since_date = date.fromisoformat(since) if since is not None else None
        until_date = date.fromisoformat(until) if until is not None else None
    except ValueError as exc:
        return {
            "status": "error",
            "message": f"Invalid date format (expected YYYY-MM-DD): {exc}",
            "orders_created": 0,
            "items_created": 0,
            "profiles_total": 0,
            "profiles_ready": 0,
            "profiles_succeeded": 0,
            "profiles_failed": 0,
            "profile_results": [],
        }

    validated_backend = cast(BackendType, backend)

    def _run() -> dict[str, Any]:
        try:
            return _scrape_amazon(
                get_db(),
                backend=validated_backend,
                since=since_date,
                until=until_date,
                max_orders=max_orders,
                profile_key=profile_key,
                fetch_item_details=fetch_item_details,
            )
        except Exception as exc:
            return {
                "status": "error",
                "message": str(exc),
                "orders_created": 0,
                "items_created": 0,
                "profiles_total": 0,
                "profiles_ready": 0,
                "profiles_succeeded": 0,
                "profiles_failed": 0,
                "profile_results": [],
            }

    return await asyncio.to_thread(_run)


@tool
async def remutate_amazon_orders(dry_run: bool = True) -> dict[str, Any]:
    """Re-split previously-persisted Plaid transactions against scraped Amazon orders.

    Destructive: matched Plaid txns have their existing derived rows
    deleted and replaced. ``dry_run`` defaults to True; inspect a dry-run
    result before applying.
    """

    def _run() -> dict[str, Any]:
        try:
            return _remutate_amazon(get_db(), dry_run=dry_run)
        except Exception as exc:
            return {
                "status": "error",
                "candidates": 0,
                "matched": 0,
                "overwrites": 0,
                "overwrite_details": [],
                "derived_after_split": 0,
                "categorized": 0,
                "dry_run": dry_run,
                "message": str(exc),
            }

    return await asyncio.to_thread(_run)
