"""Re-classify already-ingested corporate actions as DEFAULT_EXCLUDE

Revision ID: 033_exclude_corporate_actions
Revises: 032_add_reviewed_at
Create Date: 2026-09-05

``investment_activity_reporting_mode`` now treats corporate actions (splits,
spin-offs, mergers, name changes) as portfolio events rather than money
movement. That classification is stamped at ingest, so rows already in the
database keep the old verdict — they stay in spending analytics and in the
review queue, where the categorizer has already produced nonsense for them
(a spin-off labeled ``investment_buy``).

This corrects them in place. The match is deliberately narrow: only rows
whose descriptor is one of those corporate-action phrases, and only where the
current mode is not already EXCLUDE. Money movement is matched by none of
these phrases — a mortgage payment, a Zelle, or a cash transfer is untouched,
which is the property that matters most here (wrongly excluding one would
delete real spending from analytics).

Data-only and idempotent: re-running changes nothing. Categories already
assigned are left alone; an excluded row is out of analytics regardless, and
rewriting them would mean unpicking the category provenance constraint for no
gain.

DDL is static by design: migrations are frozen and never import the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "033_exclude_corporate_actions"
down_revision: str | Sequence[str] | None = "032_add_reviewed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors the corporate-action entries in EXCLUDE_KEYWORDS
# (penny/tools/_services/investment_classification.py). Phrases, never the bare
# word "split", so "Automated Payment SPLIT RENT" stays money movement.
_PHRASES = (
    "stock split",
    "reverse split",
    "split ratio",
    "spin-off",
    "spinoff",
    "spin off",
    "merger",
    "reorganization",
    "name change",
)


def upgrade() -> None:
    # An ad-hoc table construct, not the models: migrations stay frozen against
    # a schema that will keep changing around them.
    derived = sa.table(
        "derived_transactions",
        sa.column("reporting_mode", sa.String),
        sa.column("merchant_descriptor", sa.Text),
    )
    matches_phrase = sa.or_(
        *(
            sa.func.lower(derived.c.merchant_descriptor).like(f"%{phrase}%")
            for phrase in _PHRASES
        )
    )
    op.execute(
        derived.update()
        # Only rows that went through investment classification, i.e. are
        # already DEFAULT_INCLUDE. Regular card transactions carry a NULL
        # reporting_mode, and matching those would let a merchant merely NAMED
        # "Merger" or "Name Change" drop out of spending analytics.
        .where(derived.c.reporting_mode == "DEFAULT_INCLUDE")
        .where(derived.c.merchant_descriptor.is_not(None))
        .where(matches_phrase)
        .values(reporting_mode="DEFAULT_EXCLUDE")
    )


def downgrade() -> None:
    # Not reversible: the pre-migration value (NULL vs DEFAULT_INCLUDE) is not
    # recorded, and restoring the wrong one would be worse than leaving these
    # correctly excluded.
    pass
