"""``ConversationStore`` — CRUD over the app-owned conversation tables.

Self-contained SQLAlchemy over the app store's *own* engine/session
(``engine.py``). It imports neither the finance ``DB`` facade nor any agent
module — that isolation is what the segregation guardrail test enforces.

Conventions mirror the finance facade: a ``session()`` context manager that
commits on success and rolls back on error, and ``expunge`` before returning
ORM rows so callers can read them after the session closes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .engine import get_web_session_factory
from .models import Conversation, ConversationMessage, WebBase

# Title is derived from the first user message, truncated to this many chars.
_TITLE_MAX_LEN = 80


class ConversationAccessError(Exception):
    """Conversation does not exist (route → 404)."""


class ConversationStore:
    """Persistence façade for conversations and their messages."""

    def __init__(self, session_factory: Any = None) -> None:
        # Default to the process-wide app-store engine; tests may inject one.
        self._session_factory = session_factory or get_web_session_factory()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_schema(self) -> None:
        """Build the app store's tables from the models. SQLite ONLY.

        Mirrors ``engine.create_web_schema`` but honors an injected session
        factory's bind so tests can point it at a throwaway engine. On Postgres
        the tables are alembic-owned; ``create_all`` is refused there.
        """
        bind = self._session_factory.kw.get("bind")
        if bind is None:  # pragma: no cover - default factory always has a bind
            from .engine import create_web_schema

            create_web_schema()
            return
        if bind.dialect.name != "sqlite":
            raise RuntimeError(
                "create_schema()/create_all is SQLite-only; on Postgres the "
                "app tables are alembic-owned. Run `penny migrate`."
            )
        WebBase.metadata.create_all(bind)

    # ----- conversations ---------------------------------------------------

    def ensure_conversation(self, conversation_id: str) -> Conversation:
        """Return the conversation, creating it if absent."""
        with self.session() as session:
            existing = session.get(Conversation, conversation_id)
            if existing is not None:
                session.expunge(existing)
                return existing
            conv = Conversation(conversation_id=conversation_id)
            session.add(conv)
            session.flush()
            session.expunge(conv)
            return conv

    def list_conversations(self) -> list[Conversation]:
        """Return all conversations, newest-first.

        Ordered by ``updated_at`` descending so the most recently active
        conversation leads the list. Rows are expunged so callers can read
        them after the session closes.
        """
        with self.session() as session:
            rows = (
                session.query(Conversation)
                .order_by(Conversation.updated_at.desc())
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def get_conversation(self, conversation_id: str) -> Conversation:
        """Return the conversation, or raise ``ConversationAccessError``."""
        with self.session() as session:
            conv = self._require_exists(session, conversation_id)
            session.expunge(conv)
            return conv

    def set_title(self, conversation_id: str, title: str) -> None:
        """Set the conversation title (overwrites any existing title)."""
        with self.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.title = title
                conversation.updated_at = datetime.now()

    def set_title_if_unset(self, conversation_id: str, raw: str) -> None:
        """Derive + set a title from ``raw`` (first user text) only if unset.

        No-op when the conversation already has a title, so the first user
        message wins and later turns don't churn it.
        """
        title = _derive_title(raw)
        if not title:
            return
        with self.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None and not conversation.title:
                conversation.title = title
                conversation.updated_at = datetime.now()

    # ----- messages --------------------------------------------------------

    def _next_seq(self, session: Session, conversation_id: str) -> int:
        """Allocate the next ``seq`` for a conversation under the session.

        ``COALESCE(MAX(seq), -1) + 1`` so the first message is seq 0.
        """
        stmt = select(func.coalesce(func.max(ConversationMessage.seq), -1) + 1).where(
            ConversationMessage.conversation_id == conversation_id
        )
        return int(session.execute(stmt).scalar_one())

    def append_user_message(
        self,
        conversation_id: str,
        *,
        ai_sdk_message_id: str | None,
        text: str,
    ) -> int:
        """Persist a user turn; return its allocated ``seq``.

        User turns are always ``complete`` (no streaming). The ``parts`` array
        mirrors what the bridge / hydration expects for user text.
        """
        with self.session() as session:
            self._require_exists(session, conversation_id)
            seq = self._next_seq(session, conversation_id)
            session.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    ai_sdk_message_id=ai_sdk_message_id,
                    seq=seq,
                    role="user",
                    parts=[{"type": "text", "text": text}],
                    status="complete",
                )
            )
            self._touch_conversation(session, conversation_id)
            return seq

    def upsert_assistant_message(
        self,
        conversation_id: str,
        *,
        ai_sdk_message_id: str | None,
        parts: list[dict[str, Any]],
        status: str,
    ) -> int:
        """Insert or update an assistant turn, keyed by ``ai_sdk_message_id``.

        Idempotent: the same ``(conversation_id, ai_sdk_message_id)`` reconciles
        the existing row in place (e.g. the ``streaming`` placeholder is
        finalized to ``complete`` on RunEnd) rather than duplicating. Returns
        the row's ``seq``.
        """
        with self.session() as session:
            self._require_exists(session, conversation_id)
            existing = self._find_by_ai_sdk_id(
                session, conversation_id, ai_sdk_message_id
            )
            if existing is not None:
                existing.parts = parts
                existing.status = status
                existing.updated_at = datetime.now()
                seq = existing.seq
            else:
                seq = self._next_seq(session, conversation_id)
                session.add(
                    ConversationMessage(
                        conversation_id=conversation_id,
                        ai_sdk_message_id=ai_sdk_message_id,
                        seq=seq,
                        role="assistant",
                        parts=parts,
                        status=status,
                    )
                )
            self._touch_conversation(session, conversation_id)
            return seq

    def get_conversation_messages(
        self, conversation_id: str
    ) -> list[ConversationMessage]:
        """Return all messages for a conversation, ordered by seq.

        Raises ``ConversationAccessError`` if the conversation does not exist.
        """
        with self.session() as session:
            self._require_exists(session, conversation_id)
            rows = (
                session.query(ConversationMessage)
                .filter(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.seq.asc())
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def latest_activity(self, conversation_id: str) -> tuple[str, datetime] | None:
        """The (role, updated_at) of a conversation's newest message.

        A trailing ``user`` message means a turn is in flight (persisted up
        front, before dispatch); an ``assistant`` message means the turn
        finished. ``None`` when the conversation has no messages yet.
        """
        with self.session() as session:
            row = (
                session.query(ConversationMessage)
                .filter(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.seq.desc())
                .first()
            )
            return None if row is None else (row.role, row.updated_at)

    # ----- internals -------------------------------------------------------

    def _require_exists(self, session: Session, conversation_id: str) -> Conversation:
        """Load the conversation or raise ``ConversationAccessError``."""
        conv = session.get(Conversation, conversation_id)
        if conv is None:
            raise ConversationAccessError(conversation_id)
        return conv

    def _find_by_ai_sdk_id(
        self,
        session: Session,
        conversation_id: str,
        ai_sdk_message_id: str | None,
    ) -> ConversationMessage | None:
        if ai_sdk_message_id is None:
            return None
        return (
            session.query(ConversationMessage)
            .filter(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.ai_sdk_message_id == ai_sdk_message_id,
            )
            .first()
        )

    def _touch_conversation(self, session: Session, conversation_id: str) -> None:
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now()


def _derive_title(raw: str) -> str:
    """Collapse whitespace and truncate the first user message into a title."""
    collapsed = " ".join(raw.split())
    if len(collapsed) <= _TITLE_MAX_LEN:
        return collapsed
    return collapsed[: _TITLE_MAX_LEN - 1].rstrip() + "…"
