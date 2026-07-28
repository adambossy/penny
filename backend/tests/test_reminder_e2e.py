"""End-to-end reminder battery on SQLite (no Postgres, no real model).

Two invariants:

- A reminder enqueued on the DB-backed queue reaches the LLM turn: the harness
  run loop drains it into the outgoing user message and empties the queue.
- The stored conversation transcript never contains reminder text — reminders
  live only in the (unpersisted) harness turn, never in the web store the UI
  hydrates from. This is the privacy property behind the system-reminder design.

Uses a minimal in-package fake ``Model`` (the harness's own ``FakeModel`` lives
in its test tree, not the installed package) that emits one scripted text turn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from agent_harness.core.agent import Agent
from agent_harness.core.events import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    ModelEnd,
    ModelStart,
)
from agent_harness.core.models import (
    Message,
    ModelCapabilities,
    ModelSettings,
    TextBlock,
    Usage,
)
from agent_harness.sessions.inmemory import InMemorySession

from penny.api.persistence.reminders import DbReminderQueue
from tests.test_onboarding import _setup


class _FakeModel:
    """A one-turn scripted ``Model`` emitting a single assistant text message."""

    capabilities = ModelCapabilities(context_window=200_000)

    def __init__(self, text: str = "ok") -> None:
        self.name = "fake-model"
        self.provider = None  # structural check ignores type
        self._text = text

    async def request(
        self, messages: list[Message], tools: list[Any], settings: ModelSettings
    ) -> AsyncIterator[Any]:
        del messages, tools, settings
        msg_id = "msg_001"
        usage = Usage(input_tokens=10, output_tokens=2)
        yield ModelStart(model_name=self.name)
        yield MessageStart(message_id=msg_id)
        yield MessageDelta(
            message_id=msg_id,
            delta=self._text,
            partial=Message(
                role="assistant",
                content=[TextBlock(text=self._text)],
                timestamp=datetime.now(UTC),
            ),
        )
        final = Message(
            role="assistant",
            content=[TextBlock(text=self._text)],
            timestamp=datetime.now(UTC),
        )
        yield MessageEnd(message_id=msg_id, final=final, usage=usage)
        yield ModelEnd(message_id=msg_id, usage=usage)

    async def compact_messages(self, msgs: list[Message]) -> list[Message]:
        return msgs


async def test_enqueued_reminder_reaches_the_llm_turn(isolated_db):
    _setup()
    conv = "conv-e2e"
    queue = DbReminderQueue()
    await queue.enqueue(conv, "onboarding", "connect a bank")

    session = InMemorySession(session_id=conv)
    agent: Agent[Any, Any] = Agent(
        name="penny-e2e",
        model=_FakeModel(),
        toolsets=[],
        session=session,
        reminders=queue,
    )
    await agent.run("what did I spend?")

    messages = await session.get_messages()
    user = next(m for m in messages if m.role == "user")
    texts = [b.text for b in user.content if getattr(b, "text", None)]
    assert texts[0] == "what did I spend?"
    assert (
        '<system-reminder kind="onboarding">\nconnect a bank\n</system-reminder>'
        in texts
    )
    assert user.metadata["system_reminder_kinds"] == ["onboarding"]
    # The queue is emptied by the drain.
    assert await queue.drain(conv) == []


async def test_stored_conversation_never_contains_reminder_text(isolated_db):
    """The web-store user message equals exactly what the client sent.

    Reminders are appended to the harness turn (asserted above), never to the
    persisted transcript. We exercise the store's user-message persistence — the
    path /api/chat uses — with a reminder queued for the same conversation, and
    assert the stored text carries no reminder span.
    """
    from penny.api.persistence.store import ConversationStore

    _setup()
    conv = "conv-store"
    client_text = "how much did I spend on dining?"

    await DbReminderQueue().enqueue(conv, "onboarding", "connect a bank")

    store = ConversationStore()
    store.ensure_conversation(conv)
    store.append_user_message(conv, ai_sdk_message_id="u1", text=client_text)

    rows = store.get_conversation_messages(conv)
    user_rows = [r for r in rows if r.role == "user"]
    assert len(user_rows) == 1
    stored_text = "".join(
        p.get("text", "") for p in user_rows[0].parts if p.get("type") == "text"
    )
    assert stored_text == client_text
    assert "<system-reminder" not in stored_text
    # The reminder still lives in the queue — never in the transcript.
    assert [r.kind for r in await DbReminderQueue().drain(conv)] == ["onboarding"]
