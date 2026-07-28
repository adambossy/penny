"""Conversation-persistence ORM models — app bookkeeping tables.

Single-player keeps ONE database: these tables live beside the finance tables
in the same SQLite file (or Postgres DB), registered on the shared finance
``Base`` and namespaced by an ``app_`` table-name prefix (SQLite has no
schemas). ``create_all`` / the single alembic chain cover them with the rest.

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
    Mapped,
    mapped_column,
    relationship,
)

from penny.adapters.db.models import Base


class Conversation(Base):
    """A single chat conversation. PK is the client-generated UUID."""

    __tablename__ = "app_conversations"

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


class ConversationMessage(Base):
    """One message (user or assistant) with its ordered AI SDK ``parts``."""

    __tablename__ = "app_conversation_messages"
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
    )

    message_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    conversation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("app_conversations.conversation_id", ondelete="CASCADE"),
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


class QueuedReminder(Base):
    """A backend-enqueued ``<system-reminder>`` awaiting the next agent turn.

    App state: the harness drains these into the outgoing user message via the
    injected ``ReminderQueue``. ``(conversation_id, kind)`` is unique so
    ``override=True`` is an upsert; a non-override enqueue suffixes ``kind``
    (``kind#<hex>``) to append without colliding — the queue strips the suffix
    when building ``Reminder``.
    """

    __tablename__ = "app_queued_reminders"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "kind", name="uq_queued_reminders_conv_kind"
        ),
        Index("ix_queued_reminders_conversation_id", "conversation_id"),
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


class OnboardingItem(Base):
    """One progressive-onboarding step's state.

    ``status`` is the only stored state (``pending`` → ``accepted``/
    ``dismissed``); activation is *computed* per turn by the trigger engine,
    never stored. ``trigger_state`` holds the deterministic per-item
    counters/bookkeeping the engine reads (categorized-turn count, corrections,
    once-per-session stamp).
    """

    __tablename__ = "app_onboarding_items"
    __table_args__ = (
        UniqueConstraint("item_key", name="uq_onboarding_items_item"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'dismissed')",
            name="ck_onboarding_items_status",
        ),
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
