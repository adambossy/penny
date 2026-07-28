"""Single-player baseline: drop tenancy, absorb the app tables

Revision ID: 029
Revises: 028_add_eval_runs_household_id
Create Date: 2026-07-28

The single-player consolidation: one user, one database. This revision
reconciles a database built by the multi-tenant chain (through 028) to the
single-player models:

- Drops the tenant columns (``household_id`` / ``owner_user_id`` /
  ``visibility``) from every financial table, along with the tenant FKs,
  CHECKs, composite indexes, and (on Postgres) the RLS policies and the
  ``penny_set_tenant`` GUC wrapper that hung off them.
- Drops the identity/workspace tables (``households``, ``users``,
  ``workspace_prefixes``, ``workspace_manifests``, ``workspace_heads``).
- Restores the pre-tenancy uniqueness: global ``UNIQUE(name)`` on ``tags``
  and the ``uq_categories_key_active`` partial unique index on
  ``categories(key)``.
- Creates the app bookkeeping tables (``app_conversations``,
  ``app_conversation_messages``, ``app_queued_reminders``,
  ``app_onboarding_items``) on the shared finance schema — previously a
  separate web engine/schema. On Postgres the old ``web`` schema is dropped
  wholesale (the hosted product froze at 028 and never runs this revision;
  a single-player DB migrating through here consolidates into one schema).

DDL is static by design: migrations are frozen and never import the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "029"
down_revision: str | Sequence[str] | None = "028_add_eval_runs_household_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that carry the full household/owner/visibility triple (012/014).
OWNER_VIS = [
    "plaid_transactions",
    "derived_transactions",
    "transaction_items",
    "transaction_tags",
    "email_receipts",
    "pending_receipt_matches",
    "account_sign_conventions",
    "amazon_login_profiles",
    "amazon_orders",
    "amazon_items",
]
# Tables that carry household_id only (012/016/028).
HOUSEHOLD_ONLY = ["tags", "transaction_category_events", "categories", "eval_runs"]
# Identity + workspace tables owned entirely by the multi-tenant era.
TENANT_TABLES = [
    "workspace_heads",
    "workspace_manifests",
    "workspace_prefixes",
    "users",
    "households",
]
# Every table that had a tenant_isolation RLS policy on Postgres (015/016).
# eval_runs is absent: 028's household_id was eval bookkeeping, never RLS.
RLS_TABLES = [
    *OWNER_VIS,
    "plaid_accounts",
    "plaid_items",
    "tags",
    "transaction_category_events",
    "categories",
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgres()
    else:
        _upgrade_sqlite()
    _create_app_tables()


def _upgrade_postgres() -> None:
    # RLS and the set-once GUC wrapper go first; the policies reference the
    # tenant columns dropped below (CASCADE would take them anyway — explicit
    # is clearer).
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS penny_set_tenant(text, text)")

    # Tenant columns, CASCADE so dependent FKs/CHECKs/indexes
    # (fk_*_household, ck_*_visibility, ix_*_household_owner,
    # uq_tags_household_name, uq_categories_household_key_active, ...) go too.
    for table in OWNER_VIS:
        for column in ("household_id", "owner_user_id", "visibility"):
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column} CASCADE")
    for column in ("household_id", "owner_user_id"):
        op.execute(f"ALTER TABLE plaid_items DROP COLUMN IF EXISTS {column} CASCADE")
        op.execute(f"ALTER TABLE plaid_accounts DROP COLUMN IF EXISTS {column} CASCADE")
    op.execute("ALTER TABLE plaid_accounts DROP COLUMN IF EXISTS visibility CASCADE")
    for table in HOUSEHOLD_ONLY:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS household_id CASCADE")

    for table in TENANT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    # The website's separate schema consolidates into this one; the hosted
    # product froze at 028, so nothing live loses data here.
    op.execute("DROP SCHEMA IF EXISTS web CASCADE")

    # Restore the pre-tenancy uniqueness (dropped via CASCADE above).
    # tags_name_key matches Postgres's auto-name for the 000-baseline
    # UNIQUE(name), which 027 removed.
    op.execute("ALTER TABLE tags ADD CONSTRAINT tags_name_key UNIQUE (name)")
    op.execute(
        "CREATE UNIQUE INDEX uq_categories_key_active ON categories (key) "
        "WHERE deprecated_at IS NULL"
    )


def _upgrade_sqlite() -> None:
    # Standalone tenant indexes first; batch_alter_table's table rebuild does
    # not carry indexes, but dropping them explicitly keeps the schema tidy
    # even if the rebuild is skipped (e.g. a batch with only drop_column).
    for table in OWNER_VIS:
        op.drop_index(f"ix_{table}_household_owner", table_name=table)
    op.drop_index("ix_plaid_items_household_owner", table_name="plaid_items")
    op.drop_index("ix_plaid_accounts_household_owner", table_name="plaid_accounts")
    op.drop_index("ix_tags_household", table_name="tags")
    op.drop_index(
        "ix_transaction_category_events_household",
        table_name="transaction_category_events",
    )
    op.drop_index("uq_categories_household_key_active", table_name="categories")

    # Tenant constraints + columns. Constraints referencing a dropped column
    # must be removed in the same batch, or the SQLite table rebuild would
    # re-emit DDL pointing at a column that no longer exists.
    for table in OWNER_VIS:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"ck_{table}_owner_not_nil", type_="check")
            batch.drop_constraint(f"ck_{table}_visibility", type_="check")
            batch.drop_constraint(f"fk_{table}_owner_user", type_="foreignkey")
            batch.drop_constraint(f"fk_{table}_household", type_="foreignkey")
            batch.drop_column("visibility")
            batch.drop_column("owner_user_id")
            batch.drop_column("household_id")

    with op.batch_alter_table("plaid_items") as batch:
        batch.drop_constraint("ck_plaid_items_owner_not_nil", type_="check")
        batch.drop_constraint("fk_plaid_items_owner_user", type_="foreignkey")
        batch.drop_constraint("fk_plaid_items_household", type_="foreignkey")
        batch.drop_column("owner_user_id")
        batch.drop_column("household_id")

    # plaid_accounts's FKs were created inline by 011 (unnamed); reflection
    # names them per SQLite's DDL, so drop by column instead of by name.
    with op.batch_alter_table("plaid_accounts") as batch:
        batch.drop_constraint("ck_plaid_accounts_owner_not_nil", type_="check")
        batch.drop_constraint("ck_plaid_accounts_visibility", type_="check")
        batch.drop_column("visibility")
        batch.drop_column("owner_user_id")
        batch.drop_column("household_id")

    with op.batch_alter_table("tags") as batch:
        batch.drop_constraint("uq_tags_household_name", type_="unique")
        batch.drop_constraint("fk_tags_household", type_="foreignkey")
        batch.drop_column("household_id")
        batch.create_unique_constraint("uq_tags_name", ["name"])

    with op.batch_alter_table("transaction_category_events") as batch:
        batch.drop_constraint(
            "fk_transaction_category_events_household", type_="foreignkey"
        )
        batch.drop_column("household_id")

    with op.batch_alter_table("categories") as batch:
        batch.drop_constraint("fk_categories_household", type_="foreignkey")
        batch.drop_column("household_id")
    op.create_index(
        "uq_categories_key_active",
        "categories",
        ["key"],
        unique=True,
        sqlite_where=sa.text("deprecated_at IS NULL"),
    )

    # 028's column landed with no constraints or index.
    with op.batch_alter_table("eval_runs") as batch:
        batch.drop_column("household_id")

    for table in TENANT_TABLES:
        op.drop_table(table)


def _create_app_tables() -> None:
    """App bookkeeping tables, app_-prefixed on the shared schema.

    Static DDL mirroring ``penny/api/persistence/models.py`` at this revision.
    """
    op.create_table(
        "app_conversations",
        sa.Column("conversation_id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_table(
        "app_conversation_messages",
        sa.Column("message_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("ai_sdk_message_id", sa.String(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("parts", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'complete'"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app_conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('streaming', 'complete', 'error')",
            name="ck_conversation_messages_status",
        ),
    )
    op.create_index(
        "ix_app_conversation_messages_conversation_id",
        "app_conversation_messages",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conv_messages_conv_seq",
        "app_conversation_messages",
        ["conversation_id", "seq"],
    )
    op.create_index(
        "uq_conv_messages_ai_sdk_id",
        "app_conversation_messages",
        ["conversation_id", "ai_sdk_message_id"],
        unique=True,
        postgresql_where=sa.text("ai_sdk_message_id IS NOT NULL"),
        sqlite_where=sa.text("ai_sdk_message_id IS NOT NULL"),
    )

    op.create_table(
        "app_queued_reminders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "conversation_id", "kind", name="uq_queued_reminders_conv_kind"
        ),
    )
    op.create_index(
        "ix_queued_reminders_conversation_id",
        "app_queued_reminders",
        ["conversation_id"],
    )

    op.create_table(
        "app_onboarding_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("item_key", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("trigger_state", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("item_key", name="uq_onboarding_items_item"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'dismissed')",
            name="ck_onboarding_items_status",
        ),
    )


def downgrade() -> None:
    # Re-tenanting a single-player database would require re-inventing the
    # household/user identity data the upgrade destroyed; there is no way back.
    raise NotImplementedError("single-player baseline is irreversible")
