"""stream_and_persist persists finalized turns and flushes aborted ones."""

from __future__ import annotations

from pathlib import Path

from agent_harness.core.events import (
    EventBus,
    MessageDelta,
    MessageEnd,
    RunEnd,
    RunStart,
)
from agent_harness.core.models import Message, TextBlock, Usage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from penny.api.bridge import stream_and_persist
from penny.api.persistence.models import WebBase
from penny.api.persistence.store import ConversationStore


def _make_store(tmp_path: Path) -> ConversationStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    engine = engine.execution_options(schema_translate_map={"web": None})
    WebBase.metadata.create_all(engine)
    return ConversationStore(session_factory=sessionmaker(bind=engine, class_=Session))


class _FakeAgent:
    """Publishes a scripted event sequence, optionally raising before RunEnd."""

    def __init__(self, events: list[object], *, raise_after: bool) -> None:
        self._events = events
        self._raise_after = raise_after

    async def run(self, *, prompt: str, event_bus: EventBus) -> None:
        for event in self._events:
            await event_bus.publish(event)
        if self._raise_after:
            raise RuntimeError("connection dropped")


def _assistant(text: str) -> Message:
    from datetime import UTC, datetime

    return Message(
        role="assistant",
        content=[TextBlock(text=text)],
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def _drain(agen) -> list[str]:
    return [frame async for frame in agen]


async def test_stream_and_persist_finalizes_complete_turn(tmp_path: Path):
    # input: a full, clean turn
    conversation_id = "conv-ok"
    store = _make_store(tmp_path)
    store.ensure_conversation(conversation_id)
    events = [
        RunStart(run_id="run_1", agent_name="penny", prompt="q"),
        MessageDelta(message_id="m1", delta="hi", partial="hi"),
        MessageEnd(message_id="m1", final=_assistant("hi"), usage=Usage()),
        RunEnd(run_id="run_1", result=None, usage=Usage(), duration_ms=1),
    ]
    agent = _FakeAgent(events, raise_after=False)

    # act
    await _drain(
        stream_and_persist(agent, "q", store=store, conversation_id=conversation_id)
    )
    rows = store.get_conversation_messages(conversation_id)
    output = {"count": len(rows), "status": rows[0].status, "parts": rows[0].parts}

    # expected: one complete row with the finalized text
    expected_output = {
        "count": 1,
        "status": "complete",
        "parts": [{"type": "text", "text": "hi", "state": "done"}],
    }
    assert output == expected_output


async def test_stream_and_persist_flushes_aborted_turn_as_error(tmp_path: Path):
    # input: a turn that drops the connection before RunEnd
    conversation_id = "conv-abort"
    store = _make_store(tmp_path)
    store.ensure_conversation(conversation_id)
    events = [
        RunStart(run_id="run_2", agent_name="penny", prompt="q"),
        MessageDelta(message_id="m1", delta="partial", partial="partial"),
    ]
    agent = _FakeAgent(events, raise_after=True)

    # act
    frames = await _drain(
        stream_and_persist(agent, "q", store=store, conversation_id=conversation_id)
    )
    rows = store.get_conversation_messages(conversation_id)
    output = {
        "count": len(rows),
        "status": rows[0].status,
        "parts": rows[0].parts,
        "error_frame": any("connection dropped" in f for f in frames),
    }

    # expected: partial text preserved, the loop failure recorded as an error
    # part, status error, and the failure surfaced to the client as a frame.
    expected_output = {
        "count": 1,
        "status": "error",
        "parts": [
            {"type": "text", "text": "partial", "state": "done"},
            {"type": "error", "errorText": "connection dropped"},
        ],
        "error_frame": True,
    }
    assert output == expected_output
