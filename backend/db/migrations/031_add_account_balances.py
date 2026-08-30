"""Add account_balances history + plaid_accounts type/subtype

Revision ID: 031_add_account_balances
Revises: 030_add_conversation_model
Create Date: 2026-08-30

The daily balance-capture job appends one row per account per run to
``account_balances`` (append-only samples, kept forever; ``captured_at`` is
UTC — when we asked Plaid, since the bank-reported as-of time is unpopulated
for this user's institutions). ``plaid_accounts`` gains the nullable Plaid
account taxonomy (``type`` / ``subtype``) so the fact table joins a dimension
instead of denormalizing it; nullable because a stale registered account
Plaid no longer returns can never be populated.

DDL is static by design: migrations are frozen and never import the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "031_add_account_balances"
down_revision: str | Sequence[str] | None = "030_add_conversation_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plaid_accounts", sa.Column("type", sa.String(), nullable=True))
    op.add_column("plaid_accounts", sa.Column("subtype", sa.String(), nullable=True))
    op.create_table(
        "account_balances",
        sa.Column(
            "balance_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(), nullable=False),
        sa.Column(
            "current_cents",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column(
            "available_cents",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column(
            "limit_cents",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("currency", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["plaid_accounts.account_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_account_balances_account_captured",
        "account_balances",
        ["account_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_account_balances_account_captured", table_name="account_balances"
    )
    op.drop_table("account_balances")
    op.drop_column("plaid_accounts", "subtype")
    op.drop_column("plaid_accounts", "type")
