"""MessageAccumulator folds the harness event stream into final parts.

The accumulator is the inverse of bridge._translate; these tests run an event
sequence through both and assert the accumulated parts match what was streamed
(text, reasoning, tool call/result, tool-output-error, stream error).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent_harness.core.events import (
    Error,
    MessageDelta,
    MessageEnd,
    RunEnd,
    RunStart,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolCallEnd,
    ToolExecEnd,
)
from agent_harness.core.models import Message, TextBlock, Usage
from agent_harness.core.tools import ToolResult
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from penny.api.accumulator import MessageAccumulator
from penny.api.persistence.models import WebBase
from penny.api.persistence.store import ConversationStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _make_store(tmp_path: Path) -> ConversationStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    engine = engine.execution_options(schema_translate_map={"web": None})
    WebBase.metadata.create_all(engine)
    return ConversationStore(session_factory=sessionmaker(bind=engine, class_=Session))


def _assistant(text: str) -> Message:
    return Message(role="assistant", content=[TextBlock(text=text)], timestamp=_TS)


def _accumulate(events: list[object]) -> MessageAccumulator:
    acc = MessageAccumulator()
    for event in events:
        acc.consume(event)
    return acc


def test_accumulator_reasoning_text_and_tool_round_trip():
    # input: a turn that reasons, calls a tool, gets structured output, answers
    events = [
        RunStart(run_id="run_1", agent_name="penny", prompt="how many?"),
        ThinkingStart(message_id="m1"),
        ThinkingDelta(message_id="m1", delta="Let me ", partial="Let me "),
        ThinkingDelta(message_id="m1", delta="count.", partial="Let me count."),
        ThinkingEnd(message_id="m1"),
        ToolCallEnd(
            tool_call_id="c1", tool_name="run_sql", arguments={"query": "SELECT 1"}
        ),
        ToolExecEnd(
            tool_call_id="c1",
            result=ToolResult(
                content=[TextBlock(text='{"count": 6}')],
                structured_content={"count": 6},
            ),
        ),
        MessageDelta(message_id="m2", delta="You have ", partial="You have "),
        MessageDelta(message_id="m2", delta="6.", partial="You have 6."),
        MessageEnd(message_id="m2", final=_assistant("You have 6."), usage=Usage()),
        RunEnd(run_id="run_1", result=None, usage=Usage(), duration_ms=1),
    ]

    # act
    acc = _accumulate(events)
    output = {"status": acc.status, "run_id": acc.run_id, "parts": acc.parts()}

    # expected: parts in first-appearance order, finalized
    expected_output = {
        "status": "complete",
        "run_id": "run_1",
        "parts": [
            {"type": "reasoning", "text": "Let me count.", "state": "done"},
            {
                "type": "tool-run_sql",
                "toolCallId": "c1",
                "state": "output-available",
                "input": {"query": "SELECT 1"},
                "output": {"count": 6},
            },
            {"type": "text", "text": "You have 6.", "state": "done"},
        ],
    }
    assert output == expected_output


def test_accumulator_final_text_lands_after_tool_calls_with_constant_message_id():
    # Regression: Gemini reuses one message_id ("gemini-msg") for every step and
    # emits a MessageEnd per tool-only step. The final answer must render AFTER
    # the tool calls that produced it — not merged into a part pinned at step 1.
    events = [
        RunStart(run_id="run_g", agent_name="penny", prompt="report"),
        # step 1: tool only, then a step-boundary MessageEnd (no text)
        ToolCallEnd(tool_call_id="c1", tool_name="run_sql", arguments={"q": "1"}),
        ToolExecEnd(
            tool_call_id="c1",
            result=ToolResult(
                content=[TextBlock(text="a")], structured_content={"n": 1}
            ),
        ),
        MessageEnd(message_id="gemini-msg", final=_assistant(""), usage=Usage()),
        # step 2: another tool-only step, same message_id
        ToolCallEnd(tool_call_id="c2", tool_name="run_sql", arguments={"q": "2"}),
        ToolExecEnd(
            tool_call_id="c2",
            result=ToolResult(
                content=[TextBlock(text="b")], structured_content={"n": 2}
            ),
        ),
        MessageEnd(message_id="gemini-msg", final=_assistant(""), usage=Usage()),
        # step 3: the final answer, same message_id
        MessageDelta(message_id="gemini-msg", delta="Done: ", partial="Done: "),
        MessageDelta(message_id="gemini-msg", delta="2 rows.", partial="Done: 2 rows."),
        MessageEnd(
            message_id="gemini-msg", final=_assistant("Done: 2 rows."), usage=Usage()
        ),
        RunEnd(run_id="run_g", result=None, usage=Usage(), duration_ms=1),
    ]

    parts = _accumulate(events).parts()

    # The two tool parts come first, then the single text answer last.
    assert [p["type"] for p in parts] == ["tool-run_sql", "tool-run_sql", "text"]
    assert parts[-1] == {"type": "text", "text": "Done: 2 rows.", "state": "done"}


def test_accumulator_interleaved_text_segments_stay_in_transcript_order():
    # A mid-turn note ("Let me check.") and the final answer share one message_id
    # but are separated by a tool call — they must remain two parts in order,
    # not a single merged bubble.
    events = [
        RunStart(run_id="run_i", agent_name="penny", prompt="q"),
        MessageDelta(
            message_id="gemini-msg", delta="Let me check.", partial="Let me check."
        ),
        ToolCallEnd(tool_call_id="c1", tool_name="run_sql", arguments={"q": "1"}),
        ToolExecEnd(
            tool_call_id="c1",
            result=ToolResult(
                content=[TextBlock(text="a")], structured_content={"n": 1}
            ),
        ),
        MessageEnd(message_id="gemini-msg", final=_assistant(""), usage=Usage()),
        MessageDelta(message_id="gemini-msg", delta="Answer: 1.", partial="Answer: 1."),
        MessageEnd(
            message_id="gemini-msg", final=_assistant("Answer: 1."), usage=Usage()
        ),
        RunEnd(run_id="run_i", result=None, usage=Usage(), duration_ms=1),
    ]

    parts = _accumulate(events).parts()

    assert parts == [
        {"type": "text", "text": "Let me check.", "state": "done"},
        {
            "type": "tool-run_sql",
            "toolCallId": "c1",
            "state": "output-available",
            "input": {"q": "1"},
            "output": {"n": 1},
        },
        {"type": "text", "text": "Answer: 1.", "state": "done"},
    ]


def test_accumulator_tool_output_error_maps_to_output_error_part():
    # input: a tool that fails
    events = [
        RunStart(run_id="run_2", agent_name="penny", prompt="boom"),
        ToolCallEnd(tool_call_id="c1", tool_name="run_sql", arguments={"query": "x"}),
        ToolExecEnd(
            tool_call_id="c1",
            result=ToolResult(content=[TextBlock(text="boom")], error="boom"),
        ),
        RunEnd(run_id="run_2", result=None, usage=Usage(), duration_ms=1),
    ]

    # act
    acc = _accumulate(events)
    output = {"status": acc.status, "parts": acc.parts()}

    # expected: the tool part carries output-error + errorText, no output
    expected_output = {
        "status": "complete",
        "parts": [
            {
                "type": "tool-run_sql",
                "toolCallId": "c1",
                "state": "output-error",
                "input": {"query": "x"},
                "errorText": "boom",
            }
        ],
    }
    assert output == expected_output


def test_accumulator_stream_error_appends_error_part_and_sets_error_status():
    # input: a partial turn that hits a stream-level error before finishing
    events = [
        RunStart(run_id="run_3", agent_name="penny", prompt="q"),
        MessageDelta(message_id="m1", delta="partial", partial="partial"),
        Error(message="quota exceeded"),
    ]

    # act
    acc = _accumulate(events)
    output = {"status": acc.status, "parts": acc.parts()}

    # expected: partial text preserved, error part appended, status error
    expected_output = {
        "status": "error",
        "parts": [
            {"type": "text", "text": "partial", "state": "done"},
            {"type": "error", "errorText": "quota exceeded"},
        ],
    }
    assert output == expected_output


def test_accumulated_turn_persists_as_one_reconciled_row(tmp_path: Path):
    # input: a streaming placeholder followed by a finalized turn, mirroring
    # what stream_and_persist does (insert on RunStart, finalize on RunEnd).
    conversation_id = "conv-acc"
    store = _make_store(tmp_path)
    store.ensure_conversation(conversation_id)

    events = [
        RunStart(run_id="run_x", agent_name="penny", prompt="q"),
        MessageDelta(message_id="m1", delta="answer", partial="answer"),
        MessageEnd(message_id="m1", final=_assistant("answer"), usage=Usage()),
        RunEnd(run_id="run_x", result=None, usage=Usage(), duration_ms=1),
    ]

    # setup/act: feed events, persisting on RunStart (streaming) + RunEnd (final)
    acc = MessageAccumulator()
    for event in events:
        acc.consume(event)
        if isinstance(event, RunStart):
            store.upsert_assistant_message(
                conversation_id,
                ai_sdk_message_id=acc.run_id,
                parts=acc.parts(),
                status="streaming",
            )
        if isinstance(event, RunEnd):
            store.upsert_assistant_message(
                conversation_id,
                ai_sdk_message_id=acc.run_id,
                parts=acc.parts(),
                status=acc.status,
            )

    rows = store.get_conversation_messages(conversation_id)
    output = {
        "count": len(rows),
        "status": rows[0].status,
        "parts": rows[0].parts,
    }

    # expected: one reconciled row, finalized
    expected_output = {
        "count": 1,
        "status": "complete",
        "parts": [{"type": "text", "text": "answer", "state": "done"}],
    }
    assert output == expected_output
