"""Amazon mutation plugin for splitting orders into item-level transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from penny.adapters.amazon.logger import AmazonMatcherLogger
from penny.adapters.amazon.order_index import AmazonOrderIndex
from penny.adapters.amazon.plaid_matcher import (
    is_amazon_transaction,
    match_orders_to_transactions,
)
from penny.adapters.amazon.splitter import split_order_to_derived
from penny.tools._services.mutation_plugin import (
    DerivedTransactionPayload,
    MutationResult,
    TransactionItemPayload,
)

if TYPE_CHECKING:
    from penny.adapters.db.facade import DB
    from penny.adapters.db.models import DerivedTransaction, PlaidTransaction


@dataclass
class AmazonMutationPluginConfig:
    """Configuration for Amazon mutation plugin.

    Attributes:
        max_date_lag: Maximum days between order_date and posted_at (default: 30).
    """

    max_date_lag: int = 30


class AmazonMutationPlugin:
    """Mutation plugin that splits Amazon transactions by order items.

    Matches Plaid transactions to Amazon orders and splits them into
    item-level derived transactions with proportionally allocated
    tax and shipping.

    Attributes:
        name: Plugin identifier ("amazon_order_split").
        priority: Plugin priority (10, runs before default).
    """

    name: str = "amazon_order_split"
    priority: int = 10

    def __init__(self, db: DB, config: AmazonMutationPluginConfig) -> None:
        """Initialize plugin with configuration.

        Args:
            db: Database facade for loading scraped Amazon data.
            config: Plugin configuration.
        """
        self._db = db
        self._config = config
        self._index: AmazonOrderIndex | None = None
        self._plaid_id_to_order_id: dict[int, str] = {}
        self._logger = AmazonMatcherLogger()

    def initialize(self, plaid_txns: list[PlaidTransaction]) -> None:
        """Pre-compute matches for all Amazon transactions.

        Called once per batch for O(N+M) efficiency instead of O(N*M).

        Args:
            plaid_txns: All Plaid transactions in the current batch.
        """
        # Load Amazon order index from DB tables populated by browser scraping.
        self._index = AmazonOrderIndex.from_db(self._db)
        if self._index.order_count == 0:
            self._plaid_id_to_order_id = {}
            return
        self._logger.index_loaded(self._index.order_count, self._index.item_count)

        # Filter to Amazon transactions only
        amazon_txns = [
            t for t in plaid_txns if is_amazon_transaction(t.merchant_descriptor)
        ]

        if not amazon_txns:
            self._plaid_id_to_order_id = {}
            return

        self._logger.matching_start(len(amazon_txns), len(plaid_txns))

        # Match orders to transactions
        orders = list(self._index._orders.values())
        matches = match_orders_to_transactions(
            orders, amazon_txns, max_date_lag=self._config.max_date_lag
        )

        # Build reverse lookup: plaid_id -> order_id
        self._plaid_id_to_order_id = {}
        for order_id, plaid_id in matches.items():
            if plaid_id is not None:
                self._plaid_id_to_order_id[plaid_id] = order_id

    def should_handle(self, plaid_txn: PlaidTransaction) -> bool:
        """Return True if this is a matched Amazon transaction.

        Args:
            plaid_txn: The Plaid transaction to check.

        Returns:
            True if transaction was matched to an Amazon order.
        """
        return plaid_txn.plaid_transaction_id in self._plaid_id_to_order_id

    def process(
        self,
        plaid_txn: PlaidTransaction,
        old_derived: list[DerivedTransaction],
    ) -> MutationResult:
        """Split Amazon transaction into item-level derived transactions.

        Args:
            plaid_txn: The matched Amazon Plaid transaction.
            old_derived: Existing derived transactions for enrichment preservation.

        Returns:
            MutationResult with N derived transactions (one per item).
        """
        if self._index is None:
            return MutationResult(derived_data_list=[], handled=False)

        order_id = self._plaid_id_to_order_id.get(plaid_txn.plaid_transaction_id)
        if not order_id:
            return MutationResult(derived_data_list=[], handled=False)

        order = self._index.get_order(order_id)
        if not order:
            return MutationResult(derived_data_list=[], handled=False)

        # Scrape order must not change which item receives a rounding cent.
        items = sorted(self._index.get_items(order_id), key=lambda item: item.asin)

        # Historical splits used scrape order for rounding ties. Reuse that
        # order only when each item has one unambiguous existing counterpart;
        # still recompute amounts so actual price changes cannot be hidden.
        old_positions = {
            (row.items[0].description, row.items[0].quantity): row.split_index
            for row in old_derived
            if row.split_source == "amazon_mutation"
            and row.split_index is not None
            and len(row.items) == 1
            and row.items[0].source_ref == order_id
        }
        item_keys = [
            (
                item.description[:200] if item.description else "Amazon item",
                item.quantity,
            )
            for item in items
        ]
        if len(old_positions) == len(items) and set(old_positions) == set(item_keys):
            items = [
                item
                for _, item in sorted(
                    zip(item_keys, items, strict=True),
                    key=lambda pair: old_positions[pair[0]],
                )
            ]

        self._logger.match_found(
            plaid_txn.plaid_transaction_id,
            order_id,
            len(items),
            plaid_txn.amount_cents,
        )

        derived_data_list = split_order_to_derived(plaid_txn, order, items)

        # Build typed payloads from DerivedTransactionData.
        # When items are present the splitter produces one row per item; zip to
        # attach the corresponding TransactionItemPayload.  The no-items path
        # produces a single 1:1 row with no line items.
        #
        # A single UUID is generated per Amazon order so that all derived rows
        # produced by this call share the same split_group_id; split_index is
        # the 0-based position within the group.
        result_list: list[DerivedTransactionPayload] = []
        if items:
            group_id = uuid4().hex
            for split_idx, (dtd, amazon_item) in enumerate(
                zip(derived_data_list, items, strict=True)
            ):
                item_payload = TransactionItemPayload(
                    description=amazon_item.description[:200]
                    if amazon_item.description
                    else "Amazon item",
                    amount_cents=dtd.amount_cents,
                    quantity=amazon_item.quantity,
                    itemization_source="amazon_scrape",
                    source_ref=order_id,
                )
                result_list.append(
                    DerivedTransactionPayload(
                        plaid_transaction_id=dtd.plaid_transaction_id,
                        external_id=dtd.external_id,
                        amount_cents=dtd.amount_cents,
                        posted_at=dtd.posted_at,
                        merchant_descriptor=dtd.merchant_descriptor,
                        category_id=None,
                        is_verified=False,
                        items=[item_payload],
                        split_source="amazon_mutation",
                        split_group_id=group_id,
                        split_index=split_idx,
                    )
                )
        else:
            for dtd in derived_data_list:
                result_list.append(
                    DerivedTransactionPayload(
                        plaid_transaction_id=dtd.plaid_transaction_id,
                        external_id=dtd.external_id,
                        amount_cents=dtd.amount_cents,
                        posted_at=dtd.posted_at,
                        merchant_descriptor=dtd.merchant_descriptor,
                        category_id=None,
                        is_verified=False,
                        split_source="amazon_mutation",
                    )
                )

        self._logger.split_created(
            plaid_txn.plaid_transaction_id,
            len(result_list),
            [d.external_id for d in result_list],
        )

        return MutationResult(
            derived_data_list=result_list,
            handled=True,
        )
