"""Re-mutate pre-existing Plaid transactions through the Amazon split plugin.

Why this exists: the sync pipeline's mutation phase only sees Plaid txns that
were upserted in the *current* sync run. A Plaid charge that posted before a
later Amazon scrape is never re-presented to the mutation phase, so it stays
in ``derived_transactions`` as a 1:1 passthrough even though scraped order/item
data now exists for it. This module re-runs the match → split → categorize
chain over a bounded window of already-persisted Plaid txns to close that gap
(historical backfill, scrape-gap recovery, late Amazon finalization).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from penny.adapters.amazon import (
    AmazonMutationPlugin,
    AmazonMutationPluginConfig,
)
from penny.tools._services.mutation_reconciliation import (
    MutationReport,
    assess_mutation,
)

if TYPE_CHECKING:
    from penny.adapters.db.facade import DB


class SupportsRemutation(Protocol):
    """Subset of ``SyncTool`` the remutation flow drives.

    Declared as a Protocol so tests can inject a fake without constructing a
    real ``SyncTool`` (which needs Plaid + OpenAI credentials).
    """

    def _mutate_batch_to_derived(
        self, plaid_ids: list[int], *, report: MutationReport | None = None
    ) -> list[int]: ...

    async def _categorize_derived(self, derived_ids: list[int]) -> None: ...


def _default_sync_tool(db: DB) -> SupportsRemutation:
    """Build a real ``SyncTool`` (registers the Amazon mutation plugin)."""
    from penny.adapters.clients.plaid import PlaidClient
    from penny.taxonomy.loader import load_taxonomy_from_db
    from penny.tools._services.categorizer import Categorizer
    from penny.tools._services.sync_service import SyncTool

    taxonomy = load_taxonomy_from_db(db)
    return SyncTool(
        plaid_client=PlaidClient.from_env(),
        categorizer_factory=lambda: Categorizer(taxonomy),
        db=db,
        taxonomy=taxonomy,
    )


def _result(
    *,
    status: str,
    message: str,
    candidates: int = 0,
    matched: int = 0,
    overwrites: int = 0,
    overwrite_details: list[dict[str, Any]] | None = None,
    derived_after_split: int = 0,
    categorized: int = 0,
    dry_run: bool = False,
    report: MutationReport | None = None,
) -> dict[str, Any]:
    """Build the remutation result contract (flat dict, mirrors scraper)."""
    report = report or MutationReport()
    return {
        "unchanged": len(report.unchanged),
        "changed": len(report.changed),
        "mutation_conflicts": report.conflicts,
        "status": status,
        "candidates": candidates,
        "matched": matched,
        "overwrites": overwrites,
        "overwrite_details": overwrite_details or [],
        "derived_after_split": derived_after_split,
        "categorized": categorized,
        "dry_run": dry_run,
        "message": message,
    }


def remutate_amazon_orders(
    db: DB,
    *,
    dry_run: bool = False,
    sync_tool_factory: Callable[[DB], SupportsRemutation] | None = None,
) -> dict[str, Any]:
    """Re-split pre-existing Plaid txns that now match scraped Amazon orders.

    Args:
        db: Database facade.
        dry_run: When True, compute candidate/match/overwrite counts and return
            without writing anything.
        sync_tool_factory: Optional factory producing the object that performs
            mutation + categorization. Defaults to a real ``SyncTool``. Only
            invoked on a non-dry run with at least one safe change.

    Returns:
        Result-contract dict with status, counts, and overwrite details.
    """
    bounds = db.amazon_order_date_bounds()
    if bounds is None:
        return _result(
            status="noop",
            message="No scraped Amazon orders; nothing to remutate.",
            dry_run=dry_run,
        )

    lo, hi = bounds
    # Window upper bound mirrors the matcher's max_date_lag: a charge can post
    # up to N days after the order_date.
    config = AmazonMutationPluginConfig()
    window_end = hi + timedelta(days=config.max_date_lag)

    plaid_txns = db.list_plaid_transactions_in_date_range(start=lo, end=window_end)
    if not plaid_txns:
        return _result(
            status="noop",
            message=f"No Plaid transactions in [{lo}, {window_end}].",
            dry_run=dry_run,
        )

    # The plugin filters to Amazon-descriptor txns internally and matches them
    # to scraped orders by amount + date lag.
    from penny.tools._services.sync_service import _apply_sign_convention

    conventions = db.bulk_get_sign_conventions(list({t.account_id for t in plaid_txns}))
    plaid_txns = [
        _apply_sign_convention(
            t, sign_convention=conventions.get(t.account_id, "expense_positive")
        )
        for t in plaid_txns
    ]
    plugin = AmazonMutationPlugin(db, config)
    plugin.initialize(plaid_txns)
    matched_plaid_ids = sorted(
        t.plaid_transaction_id for t in plaid_txns if plugin.should_handle(t)
    )

    candidates = len(plaid_txns)
    matched = len(matched_plaid_ids)
    if not matched_plaid_ids:
        return _result(
            status="noop",
            message=f"{candidates} Plaid txns in window; none matched an order.",
            candidates=candidates,
            dry_run=dry_run,
        )

    derived_map = db.get_derived_by_plaid_ids(matched_plaid_ids)
    report = MutationReport()
    for txn in plaid_txns:
        if plugin.should_handle(txn):
            existing = derived_map.get(txn.plaid_transaction_id, [])
            proposed = plugin.process(txn, existing).derived_data_list
            assess_mutation(txn.plaid_transaction_id, existing, proposed, report)

    if not dry_run and report.changed:
        factory = sync_tool_factory or _default_sync_tool
        runner = factory(db)
        # Recheck at the mutation seam, including verification, before writing.
        report = MutationReport()
        derived_ids = runner._mutate_batch_to_derived(matched_plaid_ids, report=report)
        changed_ids = set(report.changed)
        changed_rows = [
            row
            for row in db.get_derived_transactions_by_ids(derived_ids)
            if row.plaid_transaction_id in changed_ids
        ]
        uncategorized_ids = [
            row.transaction_id for row in changed_rows if row.category_id is None
        ]
        asyncio.run(runner._categorize_derived(uncategorized_ids))
        derived_after_split = len(changed_rows)
        categorized = sum(
            row.category_id is not None
            for row in db.get_derived_transactions_by_ids(uncategorized_ids)
        )
    else:
        derived_after_split = 0
        categorized = 0

    overwrite_details = [
        {
            "plaid_transaction_id": plaid_id,
            "derived_transaction_id": row.transaction_id,
            "posted_at": row.posted_at.isoformat(),
            "amount_cents": row.amount_cents,
            "merchant_descriptor": row.merchant_descriptor,
            "category_id": row.category_id,
            "category_method": row.category_method,
            "is_verified": row.is_verified,
        }
        for plaid_id in report.changed
        for row in derived_map.get(plaid_id, [])
        if row.category_method == "manual"
    ]
    return _result(
        status="dry_run" if dry_run else ("ok" if report.changed else "noop"),
        message=(
            f"{len(report.changed)} Plaid transactions "
            f"{'would change' if dry_run else 'changed'}; "
            f"{len(report.unchanged)} unchanged; "
            f"{len(report.conflicts)} conflicts preserved for review."
        ),
        candidates=candidates,
        matched=matched,
        overwrites=len(overwrite_details),
        overwrite_details=overwrite_details,
        derived_after_split=derived_after_split,
        categorized=categorized,
        dry_run=dry_run,
        report=report,
    )
