"""ConversationStore: seq monotonicity, upsert idempotency, titling."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from penny.adapters.db.models import Base
from penny.api.persistence.store import ConversationStore


def _make_store(tmp_path: Path) -> ConversationStore:
    """Build a ConversationStore over a fresh tmp SQLite DB."""
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)
    return ConversationStore(session_factory=factory)


def _seqs_and_roles(store: ConversationStore, conversation_id: str):
    rows = store.get_conversation_messages(conversation_id)
    return [(row.seq, row.role, row.status) for row in rows]


def test_seq_is_monotonic_across_user_and_assistant(tmp_path: Path):
    # input
    conversation_id = "conv-1"

    # setup
    store = _make_store(tmp_path)
    store.ensure_conversation(conversation_id)

    # act
    store.append_user_message(conversation_id, ai_sdk_message_id="u1", text="hi")
    store.upsert_assistant_message(
        conversation_id,
        ai_sdk_message_id="run_1",
        parts=[{"type": "text", "text": "hello", "state": "done"}],
        status="complete",
    )
    store.append_user_message(conversation_id, ai_sdk_message_id="u2", text="again")
    output = _seqs_and_roles(store, conversation_id)

    # expected
    expected_output = [
        (0, "user", "complete"),
        (1, "assistant", "complete"),
        (2, "user", "complete"),
    ]
    assert output == expected_output


def test_upsert_assistant_is_idempotent_by_ai_sdk_id(tmp_path: Path):
    # input
    conversation_id = "conv-2"

    # setup: a streaming placeholder, then finalize the same run
    store = _make_store(tmp_path)
    store.ensure_conversation(conversation_id)
    store.upsert_assistant_message(
        conversation_id, ai_sdk_message_id="run_9", parts=[], status="streaming"
    )

    # act: finalize the same ai_sdk_message_id
    store.upsert_assistant_message(
        conversation_id,
        ai_sdk_message_id="run_9",
        parts=[{"type": "text", "text": "done", "state": "done"}],
        status="complete",
    )
    rows = store.get_conversation_messages(conversation_id)
    output = {
        "count": len(rows),
        "seq": rows[0].seq,
        "status": rows[0].status,
        "parts": rows[0].parts,
    }

    # expected: one row, reconciled in place
    expected_output = {
        "count": 1,
        "seq": 0,
        "status": "complete",
        "parts": [{"type": "text", "text": "done", "state": "done"}],
    }
    assert output == expected_output


def test_set_title_if_unset_keeps_first_user_message(tmp_path: Path):
    # input
    conversation_id = "conv-3"

    # setup
    store = _make_store(tmp_path)
    store.ensure_conversation(conversation_id)

    # act: derive once, then a second derive must not overwrite
    store.set_title_if_unset(conversation_id, "  How much   did I spend  ")
    store.set_title_if_unset(conversation_id, "a later turn")
    with store.session() as session:
        from penny.api.persistence.models import Conversation

        output = session.get(Conversation, conversation_id).title

    # expected: whitespace-collapsed first message wins
    expected_output = "How much did I spend"
    assert output == expected_output


def test_list_conversations_newest_first(tmp_path: Path):
    from datetime import datetime

    from penny.api.persistence.models import Conversation

    # setup: two threads with distinct updated_at
    store = _make_store(tmp_path)
    store.ensure_conversation("older")
    store.ensure_conversation("newer")

    # Stamp distinct updated_at so newest-first ordering is deterministic.
    with store.session() as session:
        session.get(Conversation, "older").updated_at = datetime(2026, 1, 1)
        session.get(Conversation, "newer").updated_at = datetime(2026, 2, 1)

    # act
    rows = store.list_conversations()
    output = [row.conversation_id for row in rows]

    # expected: newest updated_at leads
    expected_output = ["newer", "older"]
    assert output == expected_output


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
