"""Translate agent-harness events into the Vercel AI SDK UI message stream.

The frontend (``@adambossy/agent-ui`` via the AI SDK ``useChat``) consumes an
SSE stream of JSON frames tagged ``x-vercel-ai-ui-message-stream: v1``. The
harness instead publishes a typed ``Event`` union on an ``EventBus``. This
module subscribes to the bus, runs the agent, and maps each event to the
corresponding stream frame(s).

Frame sequence per turn:
    start -> start-step -> (reasoning-* | text-* | tool-input/-output)* ->
    finish-step -> finish -> [DONE]
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import json
from typing import TYPE_CHECKING, Any

from agent_harness import Agent
from agent_harness.core.events import (
    Error,
    InMemoryEventBus,
    MessageDelta,
    MessageEnd,
    MessageStart,
    RunEnd,
    RunStart,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolCallEnd,
    ToolExecEnd,
)
from loguru import logger

from penny import observability

if TYPE_CHECKING:
    from .accumulator import MessageAccumulator
    from .persistence.store import ConversationStore


def _sse(frame: dict[str, Any]) -> str:
    # default=str: a frame must NEVER kill the stream over an exotic value
    # (Decimal from Postgres numerics, UUID, datetime...). Lossy-but-readable
    # beats a silently dead SSE connection and a UI hung mid-tool-call.
    return f"data: {json.dumps(frame, default=str)}\n\n"


def _serialize_tool_output(result: Any) -> Any:
    """Convert a ``ToolResult`` to the value placed on the AI SDK frame.

    agent-harness guarantees ``result`` is a ``ToolResult`` (a dataclass with
    ``content: list[ContentBlock]`` of pydantic models, ``error``,
    ``metadata``, ``structured_content``).

    Following the MCP spec (rev 2025-11-25) ``structuredContent`` convention:
    when the tool returned structured JSON (dict/list/scalar), we emit it
    verbatim — no envelope, no string-escaped JSON inside ``content[0].text``.
    Tools that returned a bare string (or that we have no structured form for)
    fall back to the MCP-shaped content envelope so resource links, images,
    and audio blocks survive intact.
    """
    if result.structured_content is not None:
        return result.structured_content
    return {
        "content": [b.model_dump(mode="json") for b in result.content],
        "error": result.error,
        "metadata": result.metadata,
    }


def _translate(event: Any, open_text: set[str]) -> list[dict[str, Any]]:
    """Map one harness event to zero or more AI SDK stream frames.

    ``open_text`` tracks which text parts have emitted a ``text-start`` so a
    tool-only assistant message (no text deltas) never opens an empty bubble.
    """
    if isinstance(event, RunStart):
        return [{"type": "start", "messageId": event.run_id}, {"type": "start-step"}]
    if isinstance(event, ThinkingStart):
        return [{"type": "reasoning-start", "id": f"r_{event.message_id}"}]
    if isinstance(event, ThinkingDelta):
        return [
            {
                "type": "reasoning-delta",
                "id": f"r_{event.message_id}",
                "delta": event.delta,
            }
        ]
    if isinstance(event, ThinkingEnd):
        return [{"type": "reasoning-end", "id": f"r_{event.message_id}"}]
    if isinstance(event, MessageStart):
        return []  # text part opens lazily on the first delta
    if isinstance(event, MessageDelta):
        text_id = f"t_{event.message_id}"
        frames: list[dict[str, Any]] = []
        if text_id not in open_text:
            open_text.add(text_id)
            frames.append({"type": "text-start", "id": text_id})
        frames.append({"type": "text-delta", "id": text_id, "delta": event.delta})
        return frames
    if isinstance(event, MessageEnd):
        text_id = f"t_{event.message_id}"
        if text_id in open_text:
            open_text.discard(text_id)
            return [{"type": "text-end", "id": text_id}]
        return []
    if isinstance(event, ToolCallEnd):
        return [
            {
                "type": "tool-input-available",
                "toolCallId": event.tool_call_id,
                "toolName": event.tool_name,
                "input": event.arguments,
            }
        ]
    if isinstance(event, ToolExecEnd):
        # A tool that ended in error gets a distinct ``tool-output-error``
        # frame so the UI (agent-ui's DefaultTool / AI Elements' Tool
        # component pattern) can render it with the destructive tone +
        # errorText. Errors can come either as ``event.error`` (the harness
        # caught an exception) or embedded in ``event.result.error``.
        result_error = getattr(event.result, "error", None)
        err_msg = event.error or result_error
        if err_msg:
            return [
                {
                    "type": "tool-output-error",
                    "toolCallId": event.tool_call_id,
                    "errorText": str(err_msg),
                }
            ]
        return [
            {
                "type": "tool-output-available",
                "toolCallId": event.tool_call_id,
                "output": _serialize_tool_output(event.result),
            }
        ]
    if isinstance(event, RunEnd):
        return [{"type": "finish-step"}, {"type": "finish"}]
    if isinstance(event, Error):
        return [{"type": "error", "errorText": event.message}]
    return []


async def stream_agent(agent: Agent, prompt: str) -> AsyncIterator[str]:
    bus = InMemoryEventBus()
    subscription = bus.subscribe()
    # Attach the Langfuse trace builder as a second bus subscriber (no-op when
    # tracing is disabled). Subscribe before the run task starts publishing.
    trace_task = observability.start_run_trace_task(bus, source="chat", prompt=prompt)

    async def _run() -> None:
        try:
            await agent.run(prompt=prompt, event_bus=bus)
        finally:
            await bus.close()

    runner = asyncio.create_task(_run())
    open_text: set[str] = set()
    try:
        async for event in subscription:
            try:
                frames = _translate(event, open_text)
            except Exception as exc:
                # A translation bug must surface as an error frame, not kill
                # the stream silently (the UI would hang on the last frame).
                frames = [
                    {"type": "error", "errorText": f"stream translation failed: {exc}"}
                ]
            for frame in frames:
                yield _sse(frame)
    finally:
        try:
            await runner
        except Exception as exc:  # surface a loop failure to the client
            yield _sse({"type": "error", "errorText": str(exc)})
        if trace_task is not None:
            with contextlib.suppress(Exception):
                await trace_task
    yield "data: [DONE]\n\n"


async def stream_and_persist(
    agent: Agent,
    prompt: str,
    *,
    store: ConversationStore,
    conversation_id: str,
    subscribe_bus: Callable[[InMemoryEventBus], asyncio.Task[None] | None]
    | None = None,
) -> AsyncIterator[str]:
    """Stream the agent like :func:`stream_agent`, persisting the assistant turn.

    On ``RunStart`` a ``streaming`` placeholder row is inserted (enables
    mid-turn resume after a refresh). Every event is folded into a
    :class:`MessageAccumulator`; on ``RunEnd`` the row is finalized to
    ``complete``. If the stream ends without a clean finish (client disconnect,
    loop failure), the buffered partial parts are flushed with status ``error``.

    Persistence is best-effort: a store failure is logged and never raises into
    the (possibly already-closed) response.

    ``subscribe_bus`` is an optional hook the caller uses to attach an extra bus
    subscriber (the billing usage subscriber, on a subsidized run) before the
    run publishes; its returned task is awaited after the bus closes. Kept as an
    injected hook so the bridge stays billing-agnostic.
    """
    # Local import keeps the accumulator<->bridge cycle out of module init.
    from .accumulator import MessageAccumulator

    bus = InMemoryEventBus()
    subscription = bus.subscribe()
    # Langfuse trace builder as a second subscriber; conversation_id groups the
    # trace under a Langfuse session. No-op when tracing is disabled.
    trace_task = observability.start_run_trace_task(
        bus, source="chat", session_id=conversation_id, prompt=prompt
    )
    # Optional third subscriber (billing usage ledger on a subsidized run).
    extra_task = subscribe_bus(bus) if subscribe_bus is not None else None

    async def _run() -> None:
        try:
            await agent.run(prompt=prompt, event_bus=bus)
        finally:
            await bus.close()

    runner = asyncio.create_task(_run())
    open_text: set[str] = set()
    acc = MessageAccumulator()
    finalized = False
    try:
        async for event in subscription:
            acc.consume(event)
            if isinstance(event, RunStart):
                _safe_persist(store, conversation_id, acc, "streaming")
            try:
                frames = _translate(event, open_text)
            except Exception as exc:
                frames = [
                    {"type": "error", "errorText": f"stream translation failed: {exc}"}
                ]
            for frame in frames:
                yield _sse(frame)
            if isinstance(event, RunEnd):
                _safe_persist(store, conversation_id, acc, acc.status)
                finalized = True
    finally:
        try:
            await runner
        except Exception as exc:  # surface a loop failure to the client
            acc.consume(Error(message=str(exc)))
            yield _sse({"type": "error", "errorText": str(exc)})
        if not finalized:
            # Aborted / errored before RunEnd — flush partial parts as error.
            _safe_persist(store, conversation_id, acc, "error")
        if trace_task is not None:
            with contextlib.suppress(Exception):
                await trace_task
        if extra_task is not None:
            with contextlib.suppress(Exception):
                await extra_task
    yield "data: [DONE]\n\n"


def _safe_persist(
    store: ConversationStore,
    conversation_id: str,
    acc: MessageAccumulator,
    status: str,
) -> None:
    """Upsert the accumulated assistant message; never raise into the stream."""
    if acc.run_id is None:
        return
    try:
        store.upsert_assistant_message(
            conversation_id,
            ai_sdk_message_id=acc.run_id,
            parts=acc.parts(),
            status=status,
        )
    except Exception as exc:  # best-effort: log, never kill the response
        logger.bind(conversation_id=conversation_id, run_id=acc.run_id).warning(
            "Failed to persist assistant message: {}", exc
        )
