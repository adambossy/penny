"""Add the pinned model to conversations

Revision ID: 030_add_conversation_model
Revises: 029
Create Date: 2026-08-02

Each conversation is answered by exactly one model, chosen when the
conversation is created and never changed. The pin is the model-selection
composite key (model + effort, e.g. ``claude-opus-5:xhigh``), written once by
``ConversationStore.begin_turn`` on the create branch — the same write-once
shape as the title. Nullable, with no backfill: NULL means the conversation
predates model selection, and the API reports those as ``gemini-3.6-flash``
(the only model the product ran then) rather than the current default, so
history is never relabeled when the default moves.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "030_add_conversation_model"
down_revision: str | Sequence[str] | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_conversations", sa.Column("model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_conversations", "model")
