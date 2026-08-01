from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import loguru
from loguru import logger
from sqlalchemy.orm import make_transient

from penny.adapters.clients.plaid import PlaidClient, PlaidClientError
from penny.adapters.clients.plaid_models import Transaction
from penny.adapters.db.facade import DB
from penny.config import categorizer_model
from penny.normalizer import MerchantNormalizer, choose_normalizer_input
from penny.taxonomy.core import Taxonomy
from penny.tools._base import StandardTool
from penny.tools._protocol import ToolInputSchema
from penny.tools._services.categorizer import (
    Categorizer,
)
from penny.tools._services.investment_classification import (
    investment_activity_reporting_mode,
)
from penny.tools._services.mutation_plugin import DerivedTransactionPayload
from penny.tools._services.mutation_registry import MutationRegistry

if TYPE_CHECKING:
    from penny.adapters.db.models import DerivedTransaction, PlaidItem, PlaidTransaction


# Max concurrent per-descriptor categorizations during the end-of-sync sweep.
# Each is an agent run (multi-turn LLM), so this is smaller than the old batch
# fan-out.
# Concurrent categorizer agents in the end-of-sync sweep. Kept low: at 6 the
# fan-out reliably drew Gemini 503/UNAVAILABLE ("overloaded") errors that (even
# with per-call retries) left rows uncategorized. Fewer in flight → far fewer
# 503s; the sweep is cursor-independent so throughput is not the constraint.
_CATEGORIZE_CONCURRENCY = 3

# Plaid item-error codes that mean "the user must re-authenticate this bank" (as
# opposed to a transient/network blip). A sync hitting one of these reports the
# item as needing a relink rather than crashing the whole run.
_PLAID_RELINK_CODES = (
    "ITEM_LOGIN_REQUIRED",
    "PENDING_EXPIRATION",
    "INVALID_CREDENTIALS",
    "INVALID_MFA",
    "ITEM_LOCKED",
    "USER_PERMISSION_REVOKED",
)


def _relink_label(item: Any, exc: BaseException) -> str | None:
    """If ``exc`` is a Plaid re-auth error for ``item``, return a human label
    (the institution name) to report; else ``None`` (a transient/other error)."""
    if any(code in str(exc) for code in _PLAID_RELINK_CODES):
        return item.institution_name or item.item_id
    return None


# Arbitrary fixed key for the Postgres advisory lock guarding the whole sync run.
_SYNC_LOCK_KEY = 0x50454E4E59  # "PENNY"


def _apply_sign_convention(
    plaid_txn: PlaidTransaction, *, sign_convention: str
) -> PlaidTransaction:
    """Return a detached copy with amount_cents normalized per sign_convention.

    The original plaid_txn is the immutable bank truth; this copy carries the
    canonical-sign view for plugin consumption. Plugins read .amount_cents from
    the returned object without needing to know about conventions.

    make_transient() is called immediately after copy() so that the copy's
    SQLAlchemy instance state is fully detached from any session. This prevents
    attribute assignment on the copy from dirtying the original's session identity.

    Args:
        plaid_txn: The original Plaid transaction (bank truth, not modified).
        sign_convention: Either 'expense_positive' (no-op) or 'expense_negative'
            (flip sign so expenses become positive).

    Returns:
        A detached shallow copy with amount_cents reflecting canonical sign.
    """
    view = copy(plaid_txn)
    make_transient(view)
    if sign_convention == "expense_negative":
        view.amount_cents = -view.amount_cents
    return view


@dataclass
class SyncResult:
    """Result from syncing a single Plaid item."""

    removed_transaction_ids: list[str]  # Plaid transaction IDs to delete
    next_cursor: str
    has_more: bool
    added_count: int = 0
    modified_count: int = 0


@dataclass
class SyncSummary:
    """Aggregated results from syncing all Plaid items."""

    total_added: int
    total_modified: int
    total_removed: int
    items_synced: int
    investment_added: int = 0
    investment_skipped_excluded: int = 0
    investment_deduped: int = 0
    consent_required_items: list[str] | None = None
    # Institutions whose Plaid item needs re-authentication (e.g. a stale login,
    # ITEM_LOGIN_REQUIRED). A broken item is reported here, not raised — the sync
    # keeps going for the other items and still categorizes.
    relink_required_items: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        result: dict[str, Any] = {
            "total_added": self.total_added,
            "total_modified": self.total_modified,
            "total_removed": self.total_removed,
            "items_synced": self.items_synced,
            "investment_added": self.investment_added,
            "investment_skipped_excluded": self.investment_skipped_excluded,
            "investment_deduped": self.investment_deduped,
        }
        if self.consent_required_items:
            result["consent_required_items"] = self.consent_required_items
        if self.relink_required_items:
            result["relink_required_items"] = self.relink_required_items
        return result


@dataclass
class AccumulatedTransactions:
    """Accumulated transactions from all Plaid sync pages."""

    added: list[Transaction]
    modified: list[Transaction]
    removed: list[dict[str, Any]]
    final_cursor: str
    pages_fetched: int


class SyncToolLogger:
    """Handles all logging for SyncTool with business logic separated."""

    def __init__(self, logger_instance: loguru.Logger = logger) -> None:
        self._logger = logger_instance

    def fetch_start(self, cursor: str) -> None:
        """Log start of page fetch from Plaid."""
        cursor_label = cursor or "initial"
        self._logger.bind(cursor=cursor_label).info(
            "Fetching transactions from Plaid (cursor: {})", cursor_label
        )

    def fetch_complete(
        self, added_count: int, modified_count: int, removed_count: int, page_num: int
    ) -> None:
        """Log completion of page fetch."""
        self._logger.bind(
            added=added_count,
            modified=modified_count,
            removed=removed_count,
            page=page_num,
        ).info(
            "Plaid fetch complete: {} added, {} modified, {} removed (page {})",
            added_count,
            modified_count,
            removed_count,
            page_num,
        )

    def fetch_summary(
        self,
        total_added: int,
        total_modified: int,
        total_removed: int,
        total_pages: int,
    ) -> None:
        """Log summary of all fetched pages."""
        self._logger.bind(
            total_added=total_added,
            total_modified=total_modified,
            total_removed=total_removed,
            pages=total_pages,
        ).info(
            "Total fetched: {} added, {} modified, {} removed across {} pages",
            total_added,
            total_modified,
            total_removed,
            total_pages,
        )

    def mutation_retry(self, attempt: int, max_retries: int) -> None:
        """Log mutation error retry attempt."""
        self._logger.bind(attempt=attempt, max_retries=max_retries).warning(
            "Mutation detected, restarting fetch (attempt {}/{})",
            attempt,
            max_retries,
        )

    def categorization_start(
        self, total_count: int, added_count: int, modified_count: int
    ) -> None:
        """Log start of categorization phase."""
        self._logger.bind(
            total=total_count, added=added_count, modified=modified_count
        ).info(
            "Categorizing {} transactions ({} added, {} modified)",
            total_count,
            added_count,
            modified_count,
        )

    def deletion_start(self, deletion_count: int) -> None:
        """Log start of deletion phase."""
        self._logger.bind(count=deletion_count).info(
            "Deleting {} removed transactions", deletion_count
        )

    def pipeline_persist_start(self, batch_size: int, batch_num: int) -> None:
        """Log start of batch persistence in pipeline."""
        self._logger.bind(batch_size=batch_size, batch_num=batch_num).debug(
            "Persisting batch {} ({} transactions)...",
            batch_num,
            batch_size,
        )

    def pipeline_persist_complete(
        self,
        batch_size: int,
        plaid_ids_count: int,
        batch_num: int,
        elapsed_ms: int,
    ) -> None:
        """Log completion of batch persistence in pipeline."""
        self._logger.bind(
            batch_size=batch_size,
            plaid_ids=plaid_ids_count,
            batch_num=batch_num,
            elapsed_ms=elapsed_ms,
        ).debug(
            "Persisted batch {} ({} transactions → {} plaid_ids) in {}ms",
            batch_num,
            batch_size,
            plaid_ids_count,
            elapsed_ms,
        )

    def pipeline_mutate_start(self, plaid_ids_count: int, batch_num: int) -> None:
        """Log start of batch mutation in pipeline."""
        self._logger.bind(plaid_ids=plaid_ids_count, batch_num=batch_num).debug(
            "Mutating batch {} ({} plaid_ids)...",
            batch_num,
            plaid_ids_count,
        )

    def pipeline_mutate_complete(
        self,
        plaid_ids_count: int,
        derived_ids_count: int,
        batch_num: int,
        elapsed_ms: int,
    ) -> None:
        """Log completion of batch mutation in pipeline."""
        self._logger.bind(
            plaid_ids=plaid_ids_count,
            derived_ids=derived_ids_count,
            batch_num=batch_num,
            elapsed_ms=elapsed_ms,
        ).debug(
            "Mutated batch {} ({} plaid_ids → {} derived_ids) in {}ms",
            batch_num,
            plaid_ids_count,
            derived_ids_count,
            elapsed_ms,
        )


class SyncTool:
    """
    Sync tool that calls Plaid's transaction sync API and categorizes all
    results using an LLM.

    Syncs ALL connected Plaid items automatically, handling cursor persistence
    and mutation registry setup internally.
    """

    def __init__(
        self,
        plaid_client: PlaidClient,
        categorizer_factory: Callable[[], Categorizer],
        db: DB,
        taxonomy: Taxonomy,
    ) -> None:
        """
        Initialize the sync tool.

        Args:
            plaid_client: Plaid client instance
            categorizer_factory: Factory callable that creates a Categorizer on demand.
                Defers OpenAI client init, promptorium scan, and merchant rule loading
                until categorization is actually needed.
            db: Database instance for persisting transactions
            taxonomy: Taxonomy instance for transaction categorization
        """
        self._plaid_client = plaid_client
        self._categorizer_factory = categorizer_factory
        self._categorizer: Categorizer | None = None
        self._db = db
        self._taxonomy = taxonomy
        self._logger = SyncToolLogger()
        # Lazily-built merchant normalizer (defers LLM client init until a sync
        # actually resolves merchants).
        self._normalizer: MerchantNormalizer | None = None

        # Register Amazon mutation plugin; it reads scraped orders from DB tables.
        self._mutation_registry = MutationRegistry()
        from penny.adapters.amazon import (
            AmazonMutationPlugin,
            AmazonMutationPluginConfig,
        )

        self._mutation_registry.register(
            AmazonMutationPlugin(db, AmazonMutationPluginConfig())
        )

    @classmethod
    def from_env(cls) -> SyncTool:
        """The default fully-wired sync tool: env Plaid creds + the shared
        db/taxonomy/categorizer singletons. The one assembly every front door
        (agent tool, CLI, first-sync kick) uses."""
        from penny.db import get_db
        from penny.services import build_categorizer, get_taxonomy

        return cls(
            plaid_client=PlaidClient.from_env(),
            categorizer_factory=build_categorizer,
            db=get_db(),
            taxonomy=get_taxonomy(),
        )

    async def sync(
        self,
        *,
        count: int = 250,
    ) -> SyncSummary:
        """
        Sync ALL connected Plaid items with automatic pagination.

        Items are synced in parallel for maximum performance. Each item has its
        own independent cursor, so there are no cross-item dependencies.

        Items fetch/persist/mutate in parallel; categorization then runs once as a
        single end-of-sync sweep over all uncategorized rows. The whole run is
        guarded by a best-effort advisory lock so a second run (next cron tick, a
        manual sync, or a chat-driven recategorize) cannot start while agents are
        mid-flight — if the lock is already held this call is a no-op.

        Args:
            count: Maximum number of transactions per page (default: 250, max: 500)

        Returns:
            SyncSummary with aggregated results across all items

        Raises:
            PlaidClientError: If TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION occurs
                and cannot be recovered after retries
        """
        with self._db.try_advisory_lock(_SYNC_LOCK_KEY) as acquired:
            if not acquired:
                logger.info("sync skipped: another sync run holds the lock")
                return SyncSummary(
                    total_added=0, total_modified=0, total_removed=0, items_synced=0
                )

            plaid_items = self._db.list_plaid_items_for_context()
            if not plaid_items:
                return SyncSummary(
                    total_added=0, total_modified=0, total_removed=0, items_synced=0
                )

            # Fetch/persist/mutate all items in parallel (independent cursors).
            # return_exceptions=True so ONE broken item (e.g. a bank whose login
            # expired → Plaid ITEM_LOGIN_REQUIRED) can't abort the other items OR
            # the categorization sweep below — it is reported, not raised.
            tasks = [self._sync_item_with_cursor(item, count) for item in plaid_items]
            per_item = await asyncio.gather(*tasks, return_exceptions=True)

            # Flatten results and aggregate investments
            all_results: list[SyncResult] = []
            total_inv_added = 0
            total_inv_excluded = 0
            total_inv_deduped = 0
            consent_errors: list[str] = []
            relink_required: list[str] = []

            for item, outcome in zip(plaid_items, per_item, strict=True):
                if isinstance(outcome, BaseException):
                    label = _relink_label(item, outcome)
                    logger.bind(item_id=item.item_id).warning(
                        "Sync failed for item {} ({}): {}",
                        item.item_id,
                        item.institution_name,
                        outcome,
                    )
                    if label is not None and label not in relink_required:
                        relink_required.append(label)
                    continue
                results, inv_added, inv_excluded, inv_deduped, consent_error = outcome
                all_results.extend(results)
                total_inv_added += inv_added
                total_inv_excluded += inv_excluded
                total_inv_deduped += inv_deduped
                if consent_error:
                    consent_errors.append(consent_error)

            # End-of-sync categorization sweep (per-transaction agent), under lock.
            # Runs regardless of item failures — it operates on already-persisted
            # rows, not Plaid, so a broken connection must not block the backlog.
            await self._categorize_uncategorized()

            summary = self._aggregate_results(all_results, len(plaid_items))
            summary.investment_added = total_inv_added
            summary.investment_skipped_excluded = total_inv_excluded
            summary.investment_deduped = total_inv_deduped
            if consent_errors:
                summary.consent_required_items = consent_errors
            if relink_required:
                summary.relink_required_items = relink_required

            return summary

    async def _sync_item_with_cursor(
        self,
        item: PlaidItem,
        count: int,
    ) -> tuple[list[SyncResult], int, int, int, str | None]:
        """
        Sync a single Plaid item with cursor management.

        Loads cursor, syncs item, saves cursor atomically per item.
        Safe to run in parallel with other items.

        Args:
            item: PlaidItem with access_token and item_id
            count: Maximum number of transactions per page

        Returns:
            Tuple of (sync_results, investment_added, investment_excluded,
            investment_deduped, consent_error)
        """
        cursor = self._db.get_sync_cursor(item.item_id)
        results = await self._sync_item_async(
            item.access_token, item.item_id, cursor, count
        )

        # Persist cursor after successful sync
        if results:
            self._db.set_sync_cursor(item.item_id, results[-1].next_cursor)

        # Sync investments for this item (parallel operation)
        (
            inv_added,
            inv_excluded,
            inv_deduped,
            consent_error,
        ) = await self._sync_investments_for_item(item)

        return (results, inv_added, inv_excluded, inv_deduped, consent_error)

    def _aggregate_results(
        self, results: list[SyncResult], items_synced: int
    ) -> SyncSummary:
        """Aggregate results from all items into a summary."""
        return SyncSummary(
            total_added=sum(r.added_count for r in results),
            total_modified=sum(r.modified_count for r in results),
            total_removed=sum(len(r.removed_transaction_ids) for r in results),
            items_synced=items_synced,
        )

    async def _sync_investments_for_item(
        self,
        item: PlaidItem,
    ) -> tuple[int, int, int, str | None]:
        """Sync investment transactions for a single item.

        Uses watermark-based incremental sync with pagination:
        - Initial run: backfill 730 days
        - Incremental: fetch from watermark minus 7-day overlap
        - Paginated: loops through all pages until all transactions fetched
        - Dedupe by (external_id, source) handles overlaps
        - Cross-source dedup: skips PLAID_INVESTMENT rows when a matching
          PLAID row already exists (same item_id, account_id, posted_at,
          amount_cents)

        Args:
            item: PlaidItem with access_token and item_id

        Returns:
            Tuple of (added_count, excluded_count, deduped_count,
            consent_error_message).
            consent_error_message is set if ADDITIONAL_CONSENT_REQUIRED
        """
        from datetime import date, timedelta

        # Determine start date based on watermark
        if item.investments_synced_through:
            # Incremental: use watermark with 7-day overlap for safety
            start_date = item.investments_synced_through - timedelta(days=7)
        else:
            # Initial backfill: 730 days
            start_date = date.today() - timedelta(days=730)

        end_date = date.today()

        try:
            # Paginated fetch: loop through all pages
            all_investment_txns: list[dict[str, Any]] = []
            all_securities: dict[str, dict[str, Any]] = {}
            offset = 0
            page_count = 0

            while True:
                page_count += 1
                self._logger._logger.debug(
                    "Fetching investment transactions page {} (offset={}, count=500)",
                    page_count,
                    offset,
                )

                # Fetch one page of investment transactions from Plaid
                result = await asyncio.to_thread(
                    self._plaid_client.get_investment_transactions,
                    item.access_token,
                    start_date=start_date,
                    end_date=end_date,
                    count=500,
                    offset=offset,
                )

                investment_txns = result.get("investment_transactions", [])
                total_count = result.get("total_investment_transactions", 0)

                # Merge securities (dedupe by security_id)
                securities = result.get("securities", [])
                for sec in securities:
                    all_securities[sec["security_id"]] = sec

                all_investment_txns.extend(investment_txns)
                self._logger._logger.debug(
                    "Page {} fetched: {} transactions (total so far: {})",
                    page_count,
                    len(investment_txns),
                    len(all_investment_txns),
                )

                # Check if we've fetched all pages
                if not investment_txns or len(all_investment_txns) >= total_count:
                    self._logger._logger.info(
                        "Investment sync complete: {} transactions across {} pages",
                        len(all_investment_txns),
                        page_count,
                    )
                    break

                offset += 500

            investment_txns = all_investment_txns
            securities_map = all_securities

            if not investment_txns:
                # No transactions but no error - update watermark and continue
                self._db.set_investments_watermark(item.item_id, end_date)
                return (0, 0, 0, None)

            # Normalize all investment transactions first
            normalized_pairs: list[tuple[dict[str, Any], dict[str, object]]] = []
            for inv_txn in investment_txns:
                txn_dict = self._normalize_investment_transaction(
                    inv_txn, securities_map, item.item_id
                )
                normalized_pairs.append((inv_txn, txn_dict))

            # Build natural keys for cross-source dedup
            natural_keys: list[tuple[str, str, date, int]] = [
                (
                    str(txn_dict["item_id"]),
                    str(txn_dict["account_id"]),
                    cast(date, txn_dict["posted_at"]),
                    cast(int, txn_dict["amount_cents"]),
                )
                for _, txn_dict in normalized_pairs
            ]
            plaid_matches = self._db.find_plaid_matches_for_investment_dedup(
                natural_keys
            )

            # Separate duplicates from unique transactions
            skipped_dupes: list[tuple[dict[str, Any], dict[str, object]]] = []
            unique_pairs: list[tuple[dict[str, Any], dict[str, object]]] = []
            for idx, (inv_txn, txn_dict) in enumerate(normalized_pairs):
                if natural_keys[idx] in plaid_matches:
                    skipped_dupes.append((inv_txn, txn_dict))
                else:
                    unique_pairs.append((inv_txn, txn_dict))

            deduped_count = len(skipped_dupes)
            if deduped_count > 0:
                self._logger._logger.info(
                    "Skipped {} investment transactions that duplicate existing "
                    "PLAID rows for item {}",
                    deduped_count,
                    item.item_id,
                )
                self._archive_investment_dupes_to_r2(item.item_id, skipped_dupes)

            # Persist and create derived for unique transactions.
            # Look up sign conventions once for all distinct accounts in this batch.
            inv_account_ids: list[str] = list(
                {str(txn_dict["account_id"]) for _, txn_dict in unique_pairs}
            )
            inv_sign_conventions = (
                self._db.bulk_get_sign_conventions(inv_account_ids)
                if inv_account_ids
                else {}
            )

            added_count = 0
            excluded_count = 0

            for inv_txn, txn_dict in unique_pairs:
                # Persist to plaid_transactions with source=PLAID_INVESTMENT
                plaid_ids = self._db.bulk_upsert_plaid_transactions([txn_dict])

                if not plaid_ids:
                    continue

                # Create derived transaction with reporting_mode
                reporting_mode = investment_activity_reporting_mode(
                    transaction_type=inv_txn.get("type"),
                    transaction_subtype=inv_txn.get("subtype"),
                    transaction_name=inv_txn.get("name", ""),
                )

                # Apply sign convention: normalize amount_cents to canonical sign
                # (expense_positive) before storing in derived_transactions.
                raw_amount_cents = int(txn_dict["amount_cents"])  # type: ignore[arg-type]
                account_id_str = str(txn_dict["account_id"])
                inv_convention = inv_sign_conventions.get(
                    account_id_str, "expense_positive"
                )
                derived_amount_cents = (
                    -raw_amount_cents
                    if inv_convention == "expense_negative"
                    else raw_amount_cents
                )

                derived_payload = DerivedTransactionPayload(
                    plaid_transaction_id=plaid_ids[0],
                    external_id=inv_txn["investment_transaction_id"],
                    amount_cents=derived_amount_cents,
                    posted_at=txn_dict["posted_at"],  # type: ignore[arg-type]
                    merchant_descriptor=str(txn_dict["merchant_descriptor"])
                    if txn_dict.get("merchant_descriptor")
                    else None,
                    reporting_mode=reporting_mode,
                )

                # Check if derived already exists
                existing_derived = self._db.get_derived_by_plaid_ids([plaid_ids[0]])
                if existing_derived.get(plaid_ids[0]):
                    # Update existing derived with reporting_mode
                    txn_id = existing_derived[plaid_ids[0]][0].transaction_id
                    self._db.bulk_update_derived_reporting_mode(
                        {txn_id: reporting_mode}
                    )
                else:
                    # Insert new derived
                    self._db.bulk_insert_derived_transactions([derived_payload])

                if reporting_mode == "DEFAULT_EXCLUDE":
                    excluded_count += 1
                else:
                    added_count += 1

            # Update watermark after successful sync
            self._db.set_investments_watermark(item.item_id, end_date)

            return (added_count, excluded_count, deduped_count, None)

        except PlaidClientError as e:
            error_str = str(e)
            if "ADDITIONAL_CONSENT_REQUIRED" in error_str:
                # Non-fatal: return consent error message
                return (
                    0,
                    0,
                    0,
                    f"Investments consent required for item {item.item_id[:8]}...",
                )
            # Other Plaid errors should not fail the whole sync
            self._logger._logger.warning(
                "Failed to sync investments for item {}: {}", item.item_id, e
            )
            return (0, 0, 0, None)

    def _archive_investment_dupes_to_r2(
        self,
        item_id: str,
        dupes: list[tuple[dict[str, Any], dict[str, object]]],
    ) -> None:
        """Archive skipped PLAID_INVESTMENT duplicates to R2 for auditability.

        Delegates to :func:`archive_investment_dupes_to_r2` after converting
        ``(raw, normalized)`` pairs into the record format it expects.

        Args:
            item_id: Plaid item ID.
            dupes: List of (raw_plaid_dict, normalized_txn_dict) pairs.
        """
        from penny.adapters.storage.archive import (
            archive_investment_dupes_to_r2,
        )

        records: list[dict[str, Any]] = [
            {"raw": raw_txn, "normalized": dict(txn_dict)}
            for raw_txn, txn_dict in dupes
        ]
        archive_investment_dupes_to_r2(
            item_id=item_id,
            records=records,
            key_prefix="investment-dedup",
        )

    def _normalize_investment_transaction(
        self,
        inv_txn: dict[str, Any],
        securities_map: dict[str, dict[str, Any]],
        item_id: str,
    ) -> dict[str, object]:
        """Normalize investment transaction to plaid_transactions format.

        Args:
            inv_txn: Investment transaction dict from Plaid
            securities_map: Map of security_id to security details
            item_id: Plaid item ID

        Returns:
            Dict formatted for plaid_transactions table with source=PLAID_INVESTMENT
        """
        from datetime import datetime

        # Use investment_transaction_id as external_id
        external_id = inv_txn["investment_transaction_id"]

        # Parse date
        posted_at_str = inv_txn.get("date", "")
        try:
            posted_at = datetime.strptime(posted_at_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            posted_at = datetime.today().date()

        # Amount in cents
        amount = inv_txn.get("amount", 0.0)
        amount_cents = int(amount * 100)

        # Merchant descriptor: use name, fallback to security name
        merchant_descriptor = inv_txn.get("name", "")
        if not merchant_descriptor and inv_txn.get("security_id"):
            security = securities_map.get(inv_txn["security_id"])
            if security:
                merchant_descriptor = security.get("name", "")

        return {
            "external_id": external_id,
            "source": "PLAID_INVESTMENT",
            "account_id": inv_txn["account_id"],
            "item_id": item_id,
            "posted_at": posted_at,
            "amount_cents": amount_cents,
            "currency": inv_txn.get("iso_currency_code") or "USD",
            "merchant_descriptor": merchant_descriptor,
            "institution": None,
        }

    async def _categorize_derived(
        self,
        derived_ids: list[int],
    ) -> None:
        """Categorize derived transactions via the per-transaction agent.

        Only rows with ``category_id IS NULL`` are processed (brand-new rows plus
        any stranded by a prior failed run). Identical merchant descriptors are
        deduped within the run: the first is decided by ``categorize_one`` (fast
        path or agent), and the rest reuse that decision via a bulk update — so a
        merchant that appears N times costs at most one agent run.

        Args:
            derived_ids: Candidate derived transaction IDs to categorize.
        """
        from collections import defaultdict

        from penny.services import get_taxonomy
        from penny.taxonomy.loader import get_category_id
        from penny.tools._services.categorizer_agent import categorize_one

        derived_txns = self._db.get_derived_transactions_by_ids(derived_ids)
        to_categorize = [txn for txn in derived_txns if txn.category_id is None]
        if not to_categorize:
            return

        self._logger.categorization_start(len(to_categorize), len(to_categorize), 0)

        # Plaid's raw `name` lives on the plaid row; it is eager-loaded onto
        # ``txn.plaid_transaction`` by get_derived_transactions_by_ids (raw_name
        # only), so no extra fetch is needed.
        def _raw_name(txn: Any) -> str | None:
            pt = txn.plaid_transaction
            return pt.raw_name if pt is not None else None

        # Dedup by raw_name: it is the finer signal the agent actually reads and
        # it determines merchant_descriptor (which is `merchant_name or name`), so
        # a compound key would be redundant. Fall back to merchant_descriptor when
        # raw_name is absent (CSV / pre-backfill rows) so unrelated null-raw_name
        # rows don't collapse into one group. Trade-off: raw strings carrying
        # per-transaction tokens (e.g. a Zelle confirmation number) dedup less and
        # cost an extra agent run — acceptable, and it never merges distinct
        # counterparties.
        by_key: dict[str, list[Any]] = defaultdict(list)
        for txn in to_categorize:
            key = _raw_name(txn) or txn.merchant_descriptor or ""
            by_key[key].append(txn)

        semaphore = asyncio.Semaphore(_CATEGORIZE_CONCURRENCY)

        async def _handle(txns: list[Any]) -> None:
            async with semaphore:
                first = txns[0]
                decision = await categorize_one(
                    {
                        "transaction_id": first.transaction_id,
                        "merchant_descriptor": first.merchant_descriptor,
                        "raw_name": _raw_name(first),
                        "amount": first.amount_cents / 100.0,
                        "date": first.posted_at.isoformat()
                        if first.posted_at
                        else None,
                    }
                )
                category_key = decision.get("category_key")
                siblings = [txn.transaction_id for txn in txns[1:]]
                if not category_key or not siblings:
                    return
                category_id = get_category_id(self._db, get_taxonomy(), category_key)
                if category_id is None:
                    return
                self._db.bulk_update_derived_categories(
                    dict.fromkeys(siblings, category_id),
                    method="llm",
                    # Recorded on sibling rows that reuse a deduped agent
                    # decision — the model the sweep's categorizer agent ran.
                    model=categorizer_model(),
                    reason=(
                        decision.get("reasoning")
                        or "Reused the agent decision for an identical merchant "
                        "descriptor in the same sync run."
                    ),
                )

        # Resilient sweep: one transaction's failure (e.g. a transient Gemini 503
        # under concurrency) must NOT abort the whole batch. Failed groups stay
        # uncategorized and are recovered by the next sweep (which is
        # cursor-independent — it re-queries category_id IS NULL). Without this a
        # single 503 left every synced transaction uncategorized.
        results = await asyncio.gather(
            *[_handle(txns) for txns in by_key.values()], return_exceptions=True
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            logger.warning(
                "Categorization: {}/{} merchant groups failed this sweep "
                "(left uncategorized for the next run); first error: {}",
                len(failures),
                len(results),
                failures[0],
            )

    async def _categorize_uncategorized(self) -> None:
        """End-of-sync sweep: categorize every uncategorized derived row.

        Cursor-independent (queries ``category_id IS NULL`` directly), so it also
        recovers rows stranded by an earlier failed run. Runs under the sync run
        lock held by :meth:`sync`.
        """
        ids = self._db.get_uncategorized_derived_ids()
        if ids:
            await self._categorize_derived(ids)

    def _build_sync_result_from_accumulated(
        self,
        accumulated: AccumulatedTransactions,
    ) -> SyncResult:
        """Build SyncResult from accumulated data (categorization persisted to DB)."""
        removed_ids = [
            item.get("transaction_id", "")
            for item in accumulated.removed
            if item.get("transaction_id")
        ]

        return SyncResult(
            removed_transaction_ids=removed_ids,
            next_cursor=accumulated.final_cursor,
            has_more=False,
            added_count=len(accumulated.added),
            modified_count=len(accumulated.modified),
        )

    async def _sync_item_async(
        self,
        access_token: str,
        item_id: str,
        cursor: str | None,
        count: int,
    ) -> list[SyncResult]:
        """Pipelined sync: fetch, persist, mutate run concurrently.

        Pipeline stages (run concurrently):
        - fetch_producer: Fetches pages from Plaid, pushes to persist_queue
        - persist_consumer: Persists to plaid_transactions, pushes ids to mutate_queue
        - mutate_consumer: Creates derived_transactions, collects derived_ids

        Final stage (after pipeline):
        - Batch categorization of all derived transactions (for LLM efficiency)

        Args:
            access_token: Plaid access token for the item
            item_id: Plaid item ID for FK relationship
            cursor: Optional cursor for incremental sync
            count: Maximum number of transactions per page

        Returns:
            List containing single SyncResult representing entire sync

        Raises:
            PlaidClientError: If TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION occurs
                and cannot be recovered after retries
        """
        max_retries = 3
        retry_count = 0

        while retry_count <= max_retries:
            try:
                return await self._run_pipeline(access_token, item_id, cursor, count)
            except PlaidClientError as e:
                if "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" in str(e):
                    retry_count += 1
                    if retry_count <= max_retries:
                        self._logger.mutation_retry(retry_count, max_retries)
                        continue
                raise

        raise PlaidClientError(
            f"Failed to sync after {max_retries} retries due to "
            "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
        )

    async def _run_pipeline(
        self,
        access_token: str,
        item_id: str,
        start_cursor: str | None,
        count: int,
    ) -> list[SyncResult]:
        """
        Run the pipelined fetch → persist → mutate workflow.

        Args:
            access_token: Plaid access token for the item
            item_id: Plaid item ID for FK relationship
            start_cursor: Starting cursor for pagination
            count: Max transactions per page

        Returns:
            List containing single SyncResult
        """
        # Queues for pipeline stages
        persist_queue: asyncio.Queue[list[Transaction] | None] = asyncio.Queue()
        mutate_queue: asyncio.Queue[list[int] | None] = asyncio.Queue()

        # Shared state for results
        accumulated = AccumulatedTransactions(
            added=[],
            modified=[],
            removed=[],
            final_cursor="",
            pages_fetched=0,
        )
        all_derived_ids: list[int] = []
        pipeline_error: list[Exception] = []  # Mutable container for error propagation

        async def fetch_producer() -> None:
            """Fetch pages from Plaid and push to persist_queue."""
            nonlocal accumulated
            current_cursor = start_cursor
            pages_fetched = 0

            # Get excluded account_id for "CORP Account - JOIA"
            excluded_account_id = await asyncio.to_thread(
                self._get_excluded_account_id, access_token
            )
            if excluded_account_id:
                self._logger._logger.debug(
                    "Filtering transactions from account: {}", excluded_account_id
                )

            try:
                while True:
                    self._logger.fetch_start(current_cursor or "")

                    # Run sync Plaid call in thread pool to avoid blocking event loop
                    sync_result = await asyncio.to_thread(
                        self._plaid_client.sync_transactions,
                        access_token,
                        cursor=current_cursor,
                        count=count,
                    )

                    added: list[Transaction] = sync_result.get("added", [])
                    modified: list[Transaction] = sync_result.get("modified", [])
                    removed: list[dict[str, Any]] = sync_result.get("removed", [])
                    next_cursor: str = sync_result.get("next_cursor", "")
                    has_more: bool = sync_result.get("has_more", False)

                    pages_fetched += 1
                    self._logger.fetch_complete(
                        len(added), len(modified), len(removed), pages_fetched
                    )

                    # Filter out excluded account if needed
                    if excluded_account_id:
                        added = [
                            t
                            for t in added
                            if t.get("account_id") != excluded_account_id
                        ]
                        modified = [
                            t
                            for t in modified
                            if t.get("account_id") != excluded_account_id
                        ]
                        removed = [
                            t
                            for t in removed
                            if t.get("account_id") != excluded_account_id
                        ]

                    # Accumulate for result building
                    accumulated.added.extend(added)
                    accumulated.modified.extend(modified)
                    accumulated.removed.extend(removed)
                    accumulated.pages_fetched = pages_fetched
                    accumulated.final_cursor = next_cursor

                    # Push batch to persist queue
                    batch = added + modified
                    if batch:
                        await persist_queue.put(batch)

                    if not has_more:
                        self._logger.fetch_summary(
                            len(accumulated.added),
                            len(accumulated.modified),
                            len(accumulated.removed),
                            pages_fetched,
                        )
                        break

                    current_cursor = next_cursor

            except Exception as e:
                pipeline_error.append(e)
                raise
            finally:
                await persist_queue.put(None)  # Signal done

        async def persist_consumer() -> None:
            """Persist batches to plaid_transactions, push IDs to mutate_queue."""
            import time

            batch_num = 0
            try:
                while True:
                    batch = await persist_queue.get()
                    if batch is None:
                        break

                    if pipeline_error:
                        break  # Abort if fetch failed

                    batch_num += 1
                    self._logger.pipeline_persist_start(len(batch), batch_num)
                    start_time = time.monotonic()
                    plaid_ids = self._persist_batch_to_plaid(batch, item_id)
                    elapsed_ms = int((time.monotonic() - start_time) * 1000)
                    self._logger.pipeline_persist_complete(
                        len(batch),
                        len(plaid_ids),
                        batch_num,
                        elapsed_ms,
                    )
                    if plaid_ids:
                        await mutate_queue.put(plaid_ids)

            except Exception as e:
                pipeline_error.append(e)
                raise
            finally:
                await mutate_queue.put(None)  # Signal done

        async def mutate_consumer() -> None:
            """Consume from mutate_queue, create derived transactions."""
            import time

            nonlocal all_derived_ids
            batch_num = 0
            try:
                while True:
                    plaid_ids = await mutate_queue.get()
                    if plaid_ids is None:
                        break

                    if pipeline_error:
                        break  # Abort if upstream failed

                    batch_num += 1
                    self._logger.pipeline_mutate_start(len(plaid_ids), batch_num)
                    start_time = time.monotonic()
                    plaid_txns_map = self._db.get_plaid_transactions_by_ids(plaid_ids)
                    merchant_id_by_input = await self._resolve_merchant_ids(
                        list(plaid_txns_map.values())
                    )
                    derived_ids = self._mutate_batch_to_derived(
                        plaid_ids, merchant_id_by_input, plaid_txns_map
                    )
                    elapsed_ms = int((time.monotonic() - start_time) * 1000)
                    self._logger.pipeline_mutate_complete(
                        len(plaid_ids),
                        len(derived_ids),
                        batch_num,
                        elapsed_ms,
                    )
                    all_derived_ids.extend(derived_ids)

            except Exception as e:
                pipeline_error.append(e)
                raise

        # Run pipeline stages concurrently
        await asyncio.gather(
            fetch_producer(),
            persist_consumer(),
            mutate_consumer(),
        )

        # Re-raise any pipeline errors
        if pipeline_error:
            raise pipeline_error[0]

        # Handle removals
        if accumulated.removed:
            removed_ids = [
                item.get("transaction_id", "")
                for item in accumulated.removed
                if item.get("transaction_id")
            ]
            if removed_ids:
                self._logger.deletion_start(len(removed_ids))
                self._db.delete_plaid_transactions_by_external_ids(
                    removed_ids, source="PLAID"
                )

        # Categorization is no longer done per-item here. It runs once as an
        # end-of-sync sweep in sync() (over all uncategorized rows), so it is
        # deduped across items and guarded by the run lock.
        _ = all_derived_ids

        return [self._build_sync_result_from_accumulated(accumulated)]

    def _persist_batch_to_plaid(
        self, batch: list[Transaction], item_id: str
    ) -> list[int]:
        """
        Persist a batch of transactions to plaid_transactions table.

        Uses bulk upsert for performance (single DB round-trip instead of N).

        Args:
            batch: List of Plaid transactions to persist
            item_id: Plaid item ID for FK relationship

        Returns:
            List of plaid_transaction_ids that were created/updated
        """
        from datetime import datetime

        # Transform batch into dicts for bulk upsert
        txn_dicts: list[dict[str, object]] = []
        for txn in batch:
            posted_at_str = txn.get("date", "")
            try:
                posted_at = datetime.strptime(posted_at_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            amount = txn.get("amount", 0.0)
            amount_cents = int(amount * 100)

            txn_dicts.append(
                {
                    "external_id": txn.get("transaction_id", ""),
                    "source": "PLAID",
                    "account_id": txn.get("account_id", ""),
                    "item_id": item_id,
                    "posted_at": posted_at,
                    "amount_cents": amount_cents,
                    "currency": txn.get("iso_currency_code") or "USD",
                    "merchant_descriptor": txn.get("merchant_name") or txn.get("name"),
                    "original_descriptor": txn.get("original_descriptor"),
                    # Plaid's raw `name` — the fuller descriptor, kept even though
                    # merchant_descriptor prefers the cleaned merchant_name.
                    "raw_name": txn.get("name"),
                    # Plaid enrichment, stored verbatim (not surfaced to the agent).
                    "counterparties": txn.get("counterparties"),
                    "personal_finance_category": txn.get("personal_finance_category"),
                    "institution": None,
                }
            )

        if not txn_dicts:
            return []

        return self._db.bulk_upsert_plaid_transactions(txn_dicts)

    def _mutate_batch_to_derived(
        self,
        plaid_ids: list[int],
        merchant_id_by_input: dict[str, int] | None = None,
        plaid_txns_map: dict[int, PlaidTransaction] | None = None,
    ) -> list[int]:
        """
        Create derived transactions for a batch of plaid_transaction_ids.

        Uses the mutation registry to process transactions. Plugins like
        AmazonMutationPlugin can split transactions into multiple derived
        transactions. Default behavior is 1:1 mapping.

        Args:
            plaid_ids: List of plaid_transaction_ids to process
            merchant_id_by_input: Optional pre-resolved map of normalizer-input
                string -> merchant_id (produced by :meth:`_resolve_merchant_ids`
                in the async pipeline). When provided, each 1:1 payload's
                merchant_id is stamped from it so the facade skips its naive
                merchant resolution. When omitted (re-derive / remutate / tests),
                the facade resolves merchants the legacy way.
            plaid_txns_map: Optional pre-fetched plaid rows for ``plaid_ids``
                (the async pipeline already fetched them to resolve merchants).
                Fetched here when omitted.

        Returns:
            List of derived transaction_ids that were created
        """
        # Batch fetch all plaid transactions and old derived in 2 queries
        if plaid_txns_map is None:
            plaid_txns_map = self._db.get_plaid_transactions_by_ids(plaid_ids)
        old_derived_map = self._db.get_derived_by_plaid_ids(plaid_ids)

        # Look up sign conventions once per batch — one DB round-trip
        account_ids: list[str] = list(
            {txn.account_id for txn in plaid_txns_map.values()}
        )
        sign_conventions = (
            self._db.bulk_get_sign_conventions(account_ids) if account_ids else {}
        )

        # Build normalized views upfront so both initialize_plugins and the
        # per-transaction loop use the same canonical-sign copies.
        # Amazon matching hashes on amount_cents, so plugins must receive the
        # same normalized view that process() will use — otherwise expense_negative
        # accounts (stored as negatives) would never match positive order totals.
        normalized_views: dict[int, PlaidTransaction] = {}
        for pid in plaid_ids:
            plaid_txn = plaid_txns_map.get(pid)
            if plaid_txn is None:
                continue
            convention = sign_conventions.get(plaid_txn.account_id)
            if convention is None:
                logger.bind(account_id=plaid_txn.account_id).warning(
                    "No sign convention found for account {}; defaulting to "
                    "expense_positive",
                    plaid_txn.account_id,
                )
                convention = "expense_positive"
            normalized_views[pid] = _apply_sign_convention(
                plaid_txn, sign_convention=convention
            )

        # Initialize plugins with normalized views for O(N+M) matching efficiency.
        # Plugins must see canonical-sign amounts so amount-based matching works
        # correctly for expense_negative accounts.
        normalized_list = [
            normalized_views[pid] for pid in plaid_ids if pid in normalized_views
        ]
        self._mutation_registry.initialize_plugins(normalized_list)

        # Collect all derived data and plaid_ids to delete
        all_new_derived_data: list[DerivedTransactionPayload] = []
        plaid_ids_to_delete: list[int] = []
        unchanged_derived_ids: list[int] = []

        for plaid_id in plaid_ids:
            if plaid_id not in plaid_txns_map:
                continue

            old_derived = old_derived_map.get(plaid_id, [])

            # Guard verified rows: if any old derived row is verified, skip the
            # cascade delete. Destroying verified rows on re-sync would silently
            # drop data the user manually validated.
            if old_derived and any(row.is_verified for row in old_derived):
                verified_count = sum(1 for row in old_derived if row.is_verified)
                logger.bind(
                    plaid_transaction_id=plaid_id,
                    verified_count=verified_count,
                ).warning(
                    "skipping re-derive for plaid_transaction_id={}; {} verified "
                    "rows would be affected; verified rows are immutable.",
                    plaid_id,
                    verified_count,
                )
                unchanged_derived_ids.extend(row.transaction_id for row in old_derived)
                continue

            normalized_txn = normalized_views[plaid_id]

            # Use registry to process (returns N derived for plugins, 1 for default)
            result = self._mutation_registry.process(normalized_txn, old_derived)

            # Skip delete+reinsert when derived data is unchanged (1:1 only)
            if (
                old_derived
                and len(old_derived) == 1
                and len(result.derived_data_list) == 1
                and self._derived_unchanged(old_derived[0], result.derived_data_list[0])
            ):
                unchanged_derived_ids.append(old_derived[0].transaction_id)
                continue

            all_new_derived_data.extend(result.derived_data_list)

            # Mark for deletion if there were old derived
            if old_derived:
                plaid_ids_to_delete.append(plaid_id)

        # Stamp pre-resolved merchant_ids onto 1:1 payloads (when the async
        # pipeline supplied a resolution map). Each payload's normalizer input
        # is recomputed from its descriptor + the plaid row's original_descriptor
        # so it matches the keys produced by _resolve_merchant_ids. Split payloads
        # (e.g. Amazon items) carry their own descriptors and fall through to the
        # facade's naive resolution, which is correct for them.
        if merchant_id_by_input:
            stamped: list[DerivedTransactionPayload] = []
            for p in all_new_derived_data:
                if p.merchant_id is not None or not p.merchant_descriptor:
                    stamped.append(p)
                    continue
                plaid_txn = plaid_txns_map.get(p.plaid_transaction_id)
                original_descriptor = (
                    plaid_txn.original_descriptor if plaid_txn is not None else None
                )
                chosen = choose_normalizer_input(
                    p.merchant_descriptor, original_descriptor
                )
                mid = merchant_id_by_input.get(chosen)
                stamped.append(replace(p, merchant_id=mid) if mid is not None else p)
            all_new_derived_data = stamped

        # Bulk delete old derived in single query
        if plaid_ids_to_delete:
            self._db.delete_derived_by_plaid_ids(plaid_ids_to_delete)

        # Bulk insert all new derived in single call
        new_ids = self._db.bulk_insert_derived_transactions(all_new_derived_data)
        return unchanged_derived_ids + new_ids

    def _get_normalizer(self) -> MerchantNormalizer:
        if self._normalizer is None:
            self._normalizer = MerchantNormalizer()
        return self._normalizer

    async def _resolve_merchant_ids(
        self, plaid_txns: list[PlaidTransaction]
    ) -> dict[str, int]:
        """Pre-resolve merchants for a batch via the LLM normalizer (async).

        For each plaid row, the normalizer input is chosen from its
        merchant_descriptor and original_descriptor (the latter carries the
        counterparty for wrapper merchants like Venmo). Distinct inputs are
        normalized in one batched call and each resolved identity is upserted to
        a merchant_id. Returns a map of input string -> merchant_id that
        _mutate_batch_to_derived stamps onto matching 1:1 payloads. Inputs the
        LLM failed to resolve are simply absent, so those payloads fall back to
        the facade's merchant_descriptor-based naive resolution.
        """
        inputs = {
            choose_normalizer_input(txn.merchant_descriptor, txn.original_descriptor)
            for txn in plaid_txns
        }
        distinct_inputs = [s for s in inputs if s]
        if not distinct_inputs:
            return {}

        resolved = await self._get_normalizer().normalize_many(distinct_inputs)
        merchant_id_by_input: dict[str, int] = {}
        for descriptor, merchant in resolved.items():
            merchant_id_by_input[descriptor] = self._db.get_or_create_merchant_id(
                normalized_name=merchant.normalized_name,
                display_name=merchant.display_name,
                source_channel=merchant.source_channel,
                counterparty=merchant.counterparty,
            )
        return merchant_id_by_input

    @staticmethod
    def _derived_unchanged(
        old: DerivedTransaction,
        new_payload: DerivedTransactionPayload,
    ) -> bool:
        """Check if derived transaction data is unchanged.

        Compares the core fields that come from Plaid (amount, date,
        merchant descriptor). If all match, the mutation can be skipped.
        """
        return (
            old.amount_cents == new_payload.amount_cents
            and old.posted_at == new_payload.posted_at
            and old.merchant_descriptor == new_payload.merchant_descriptor
        )

    def _get_excluded_account_id(self, access_token: str) -> str | None:
        """Get account_id for 'CORP Account - JOIA' if it exists.

        Args:
            access_token: Plaid access token for the item

        Returns:
            Account ID if found, None otherwise
        """
        try:
            accounts = self._plaid_client.get_accounts(access_token)
            for account in accounts:
                if account.get("name") == "CORP Account - JOIA":
                    return account["account_id"]
        except Exception as e:
            # If we can't fetch accounts, don't filter (fail open)
            self._logger._logger.debug("Failed to fetch accounts for filtering: {}", e)
        return None


class SyncTransactionsTool(StandardTool):
    """
    Tool wrapper for syncing transactions via Plaid.

    Exposes the SyncTool functionality through the standardized Tool protocol
    for use across multiple frontends (CLI, ChatKit, MCP, etc.).
    """

    _name = "sync_transactions"
    _description = (
        "Trigger synchronization with Plaid to fetch latest transactions. "
        "Syncs ALL connected Plaid items with automatic pagination, "
        "categorizes transactions, and persists results to the database."
    )
    _input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {},  # No parameters needed - syncs all items automatically
        "required": [],
    }

    def __init__(
        self,
        plaid_client: PlaidClient,
        categorizer_factory: Callable[[], Categorizer],
        db: DB,
        taxonomy: Taxonomy,
    ) -> None:
        """
        Initialize the sync transactions tool.

        Args:
            plaid_client: Plaid client instance
            categorizer_factory: Factory callable that creates a Categorizer on demand
            db: Database instance for persisting transactions
            taxonomy: Taxonomy instance for transaction categorization
        """
        self._sync_tool = SyncTool(
            plaid_client=plaid_client,
            categorizer_factory=categorizer_factory,
            db=db,
            taxonomy=taxonomy,
        )

    async def _execute_impl(self, **kwargs: Any) -> dict[str, Any]:
        """
        Execute sync and return summary dict.

        Returns:
            JSON-serializable dict with sync results including:
            - status: "success" or "error"
            - items_synced: Number of Plaid items synced
            - total_added: Total transactions added
            - total_modified: Total transactions modified
            - total_removed: Total transactions removed
        """
        try:
            summary = await self._sync_tool.sync()
            return {"status": "success", **summary.to_dict()}
        except PlaidClientError as e:
            return {
                "status": "error",
                "error": f"Plaid sync failed: {e}",
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Sync failed: {e}",
            }
