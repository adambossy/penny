"""Decide whether proposed source data changes an existing transaction group.

User enrichments and generated split positions are deliberately excluded. A
no-op keeps the original rows (and all their attached user data) intact.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from penny.adapters.db.models import DerivedTransaction
from penny.tools._services.mutation_plugin import DerivedTransactionPayload


@dataclass
class MutationReport:
    unchanged: list[int] = field(default_factory=list)
    changed: list[int] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _source_data(row: DerivedTransaction | DerivedTransactionPayload) -> tuple:
    items = Counter(
        (
            item.source_ref,
            item.itemization_source,
            item.description,
            item.quantity,
            item.amount_cents,
        )
        # Unsplit rows can carry user-added receipt items; the default Plaid
        # mutation does not own those annotations.
        for item in (row.items or [])
        if row.split_source is not None
    )
    return (
        row.amount_cents,
        row.posted_at,
        row.merchant_descriptor,
        row.split_source,
        frozenset(items.items()),
    )


def assess_mutation(
    plaid_id: int,
    existing: list[DerivedTransaction],
    proposed: list[DerivedTransactionPayload],
    report: MutationReport,
) -> bool:
    """Record one group's decision; return whether replacement is allowed."""
    if existing and Counter(map(_source_data, existing)) == Counter(
        map(_source_data, proposed)
    ):
        report.unchanged.append(plaid_id)
        return False
    protected = [row for row in existing if row.is_verified]
    if protected:
        report.conflicts.append(
            {
                "plaid_transaction_id": plaid_id,
                "reason": "source_changed_with_verified_rows",
                "derived_transaction_ids": [row.transaction_id for row in existing],
                "verified_transaction_ids": [row.transaction_id for row in protected],
                "existing": [
                    {
                        **_describe(row),
                        "transaction_id": row.transaction_id,
                        "category_id": row.category_id,
                        "is_verified": row.is_verified,
                    }
                    for row in existing
                ],
                "proposed": [_describe(row) for row in proposed],
            }
        )
        return False
    report.changed.append(plaid_id)
    return True


def _describe(row: DerivedTransaction | DerivedTransactionPayload) -> dict[str, Any]:
    return {
        "amount_cents": row.amount_cents,
        "posted_at": row.posted_at.isoformat(),
        "merchant_descriptor": row.merchant_descriptor,
        "split_source": row.split_source,
        "items": [
            {
                "source_ref": item.source_ref,
                "itemization_source": item.itemization_source,
                "description": item.description,
                "quantity": item.quantity,
                "amount_cents": item.amount_cents,
            }
            for item in row.items or []
        ],
    }
