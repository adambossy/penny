"""Conversation-persistence ORM models on the app store's own ``Base``.

These tables are deliberately **not** registered on the finance
``penny.adapters.db.models.Base``. Keeping them on a separate declarative base
(and a separate engine — see ``engine.py``) keeps app bookkeeping distinct
from the finance schema.

A message stores its ordered AI SDK ``parts`` as a single JSON array column —
the natural read/write unit is the whole UIMessage, and we never query across
individual parts.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# Logical schema name for the app tables. On Postgres the engine maps this to
# a real ``web`` schema; on SQLite the engine maps it to ``None`` (SQLite has
# no schemas). See ``engine.py``.
WEB_SCHEMA = "web"


class WebBase(DeclarativeBase):
    """Declarative base for app-owned (non-finance) tables."""

    pass


class Conversation(WebBase):
    """A single chat conversation. PK is the client-generated UUID."""

    __tablename__ = "conversations"
    __table_args__ = {"schema": WEB_SCHEMA}

    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    messages: Mapped[list[ConversationMessage]] = relationship(
        "ConversationMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class ConversationMessage(WebBase):
    """One message (user or assistant) with its ordered AI SDK ``parts``."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('streaming', 'complete', 'error')",
            name="ck_conversation_messages_status",
        ),
        Index(
            "ix_conv_messages_conv_seq",
            "conversation_id",
            "seq",
        ),
        Index(
            "uq_conv_messages_ai_sdk_id",
            "conversation_id",
            "ai_sdk_message_id",
            unique=True,
            postgresql_where=text("ai_sdk_message_id IS NOT NULL"),
            sqlite_where=text("ai_sdk_message_id IS NOT NULL"),
        ),
        {"schema": WEB_SCHEMA},
    )

    message_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    conversation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey(f"{WEB_SCHEMA}.conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The Vercel AI SDK (useChat) message id: run_id for assistant turns, the
    # client-minted UUID for user turns. Drives idempotent upsert.
    ai_sdk_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # user / assistant
    parts: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'complete'")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="messages"
    )


class QueuedReminder(WebBase):
    """A backend-enqueued ``<system-reminder>`` awaiting the next agent turn.

    App state: the harness drains these into the outgoing user message via the
    injected ``ReminderQueue``. ``(conversation_id, kind)`` is unique so
    ``override=True`` is an upsert; a non-override enqueue suffixes ``kind``
    (``kind#<hex>``) to append without colliding — the queue strips the suffix
    when building ``Reminder``.
    """

    __tablename__ = "queued_reminders"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "kind", name="uq_queued_reminders_conv_kind"
        ),
        Index("ix_queued_reminders_conversation_id", "conversation_id"),
        {"schema": WEB_SCHEMA},
    )

    # Autoincrement integer PK so ``ORDER BY id`` is exact insertion order —
    # reliable FIFO drain even when several reminders land in the same clock
    # second; mirrors the sibling tables' surrogate keys.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class OnboardingItem(WebBase):
    """One progressive-onboarding step's state.

    ``status`` is the only stored state (``pending`` → ``accepted``/
    ``dismissed``); activation is *computed* per turn by the trigger engine,
    never stored. ``trigger_state`` holds the deterministic per-item
    counters/bookkeeping the engine reads (categorized-turn count, corrections,
    once-per-session stamp).
    """

    __tablename__ = "onboarding_items"
    __table_args__ = (
        UniqueConstraint("item_key", name="uq_onboarding_items_item"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'dismissed')",
            name="ck_onboarding_items_status",
        ),
        {"schema": WEB_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )
    trigger_state: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
