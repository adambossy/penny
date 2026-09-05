"""Add derived_transactions.reviewed_at (the golden-dataset marker)

Revision ID: 032_add_reviewed_at
Revises: 031_add_account_balances
Create Date: 2026-09-04

The review page records human category labels, which are the only ground truth
the categorizer can be scored against (agent-vs-agent replay measures nothing —
prod's sync categorizer and the eval replay are the same code path).

``is_verified`` cannot mark those labels: the categorizer's fast path also sets
it when it reuses a verified descriptor, so a verified row may never have been
seen by a person. ``reviewed_at`` is set only by the review page, so the
accuracy denominator is exactly the rows a human settled.

Nullable, no backfill: every pre-existing row reads as unreviewed, which is
true — it enters the review queue.

DDL is static by design: migrations are frozen and never import the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "032_add_reviewed_at"
down_revision: str | Sequence[str] | None = "031_add_account_balances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "derived_transactions",
        sa.Column("reviewed_at", sa.TIMESTAMP(), nullable=True),
    )
    # The review queue is "unreviewed, newest first" — a partial index keeps it
    # cheap as the reviewed set grows past the pending one.
    op.create_index(
        "idx_derived_transactions_pending_review",
        "derived_transactions",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("reviewed_at IS NULL"),
        sqlite_where=sa.text("reviewed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_derived_transactions_pending_review", table_name="derived_transactions"
    )
    op.drop_column("derived_transactions", "reviewed_at")
