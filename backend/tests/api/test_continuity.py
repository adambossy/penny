"""Continuity into the model survives persist_session=False.

The gate for flipping persist_session=False: turn 1 persists to the app store;
turn 2 — with persist_session=False — seeds a fresh agent from the app store and
the model SEES turn 1's context (user text, the tool-call/result round-trip, and
the assistant answer), not merely the UI re-render. Covers a tool-call turn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_harness import Agent, StaticToolset, tool
from agent_harness.core.events import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    ModelEnd,
    ModelStart,
    ToolCallEnd,
    ToolCallStart,
)
from agent_harness.core.models import (
    Message,
    ModelCapabilities,
    ModelSettings,
    TextBlock,
    ToolCallBlock,
    Usage,
)
from agent_harness.core.tools import ToolCall
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from penny.api.bridge import stream_and_persist
from penny.api.persistence.models import WebBase
from penny.api.persistence.rehydrate import parts_to_messages
from penny.api.persistence.store import ConversationStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)


@tool
async def echo(value: str) -> dict[str, Any]:
    """Echo the value back."""
    return {"echoed": value}


class _ScriptedModel:
    """Emits scripted turns; records the messages handed to each request."""

    capabilities = ModelCapabilities(
        parallel_tool_calls=True, structured_output=True, context_window=200_000
    )

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.name = "scripted"
        self.provider = None  # structural Model check ignores the type
        self._turns = turns
        self._i = 0
        self.seen_messages: list[list[Message]] = []

    async def request(
        self,
        messages: list[Message],
        tools: list[Any],
        settings: ModelSettings,
    ) -> AsyncIterator[Any]:
        del tools, settings
        self.seen_messages.append(list(messages))
        turn = self._turns[self._i]
        self._i += 1
        msg_id = f"msg_{self._i}"
        yield ModelStart(model_name=self.name)
        yield MessageStart(message_id=msg_id)
        text = turn.get("text", "")
        if text:
            yield MessageDelta(
                message_id=msg_id,
                delta=text,
                partial=Message(
                    role="assistant", content=[TextBlock(text=text)], timestamp=_TS
                ),
            )
        tool_calls: list[ToolCall] = turn.get("tool_calls", [])
        for tc in tool_calls:
            yield ToolCallStart(tool_call_id=tc.id, tool_name=tc.name)
            yield ToolCallEnd(
                tool_call_id=tc.id, tool_name=tc.name, arguments=tc.arguments
            )
        content: list[Any] = [TextBlock(text=text)] if text else []
        content.extend(
            ToolCallBlock(id=tc.id, name=tc.name, arguments=tc.arguments)
            for tc in tool_calls
        )
        final = Message(role="assistant", content=content, timestamp=_TS)
        yield MessageEnd(message_id=msg_id, final=final, usage=Usage())
        yield ModelEnd(message_id=msg_id, usage=Usage())

    async def compact_messages(self, msgs: list[Message]) -> list[Message]:
        return list(msgs)


def _make_store(tmp_path: Path) -> ConversationStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    engine = engine.execution_options(schema_translate_map={"web": None})
    WebBase.metadata.create_all(engine)
    return ConversationStore(session_factory=sessionmaker(bind=engine, class_=Session))


def _build_agent(model: Any, session: Any, *, persist_session: bool) -> Agent:
    return Agent(
        name="penny-test",
        model=model,
        instructions="You are a test agent.",
        session=session,
        persist_session=persist_session,
        toolsets=[StaticToolset(name="t", tools=[echo])],
    )


async def _drain(agen: AsyncIterator[str]) -> None:
    async for _ in agen:
        pass


async def test_turn2_model_sees_turn1_tool_call_with_persist_session_off(
    tmp_path: Path,
):
    # input: turn 1 makes a tool call then answers; persisted to the app store.
    conversation_id = "conv-cont"
    store = _make_store(tmp_path)
    store.ensure_conversation(conversation_id)
    store.append_user_message(
        conversation_id, ai_sdk_message_id="u1", text="echo hello"
    )

    turn1_model = _ScriptedModel(
        [
            {
                "tool_calls": [
                    ToolCall(id="c1", name="echo", arguments={"value": "hello"})
                ]
            },
            {"text": "I echoed hello."},
        ]
    )
    # Turn 1 runs with persist_session True is irrelevant; the app store capture
    # happens at the bridge. Use an in-memory session for the harness side.
    from agent_harness.sessions.inmemory import InMemorySession

    agent1 = _build_agent(
        turn1_model, InMemorySession(session_id=conversation_id), persist_session=False
    )
    await _drain(
        stream_and_persist(
            agent1, "echo hello", store=store, conversation_id=conversation_id
        )
    )

    # setup: seed turn 2 from the app store (prior turns only), then append the
    # new user turn — exactly what main.chat does.
    rows_before = store.get_conversation_messages(conversation_id)
    seed_messages = parts_to_messages(rows_before)
    turn2_session = InMemorySession(session_id=conversation_id)
    await turn2_session.add_messages(seed_messages)
    store.append_user_message(
        conversation_id, ai_sdk_message_id="u2", text="what did you echo?"
    )

    turn2_model = _ScriptedModel([{"text": "I previously echoed hello."}])
    agent2 = _build_agent(turn2_model, turn2_session, persist_session=False)

    # act
    await _drain(
        stream_and_persist(
            agent2,
            "what did you echo?",
            store=store,
            conversation_id=conversation_id,
        )
    )

    # expected: the model's first request on turn 2 SEES turn 1's full context.
    seen = turn2_model.seen_messages[0]
    roles = [m.role for m in seen]
    texts = [m.text for m in seen]
    tool_call_names = [
        block.name
        for m in seen
        for block in m.content
        if isinstance(block, ToolCallBlock)
    ]
    tool_result_contents = [
        block.content for m in seen if m.role == "tool" for block in m.content
    ]

    output = {
        "has_turn1_user": "echo hello" in texts,
        "has_turn1_answer": "I echoed hello." in texts,
        "has_turn2_user": "what did you echo?" in texts,
        "saw_tool_call": "echo" in tool_call_names,
        "tool_result_echoes_hello": any(
            "hello" in str(c) for c in tool_result_contents
        ),
        "has_tool_role": "tool" in roles,
    }
    expected_output = {
        "has_turn1_user": True,
        "has_turn1_answer": True,
        "has_turn2_user": True,
        "saw_tool_call": True,
        "tool_result_echoes_hello": True,
        "has_tool_role": True,
    }
    assert output == expected_output
    # And the new user prompt is not duplicated by the seed + loop append.
    assert texts.count("what did you echo?") == 1
