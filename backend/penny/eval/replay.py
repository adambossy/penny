"""Replay the categorizer on one transaction and capture its decision.

Runs against whatever ``get_db()`` currently points at (a backtest points it at
the disposable branch). Captures what the DB never stores — confidence, the tools
consulted, and the Langfuse trace link — by reading the agent's
``submit_categorization`` call, mirroring the review-harness capture.

The trace link is the *canonical* Langfuse trace URL — the id Langfuse stores
the trace under, sourced from the Langfuse SDK via
``observability.current_trace_url()`` (see :mod:`penny.observability.otel`).
"""

from __future__ import annotations

import contextlib
from typing import Any


def _extract_from_result(result: Any) -> tuple[list[str], dict[str, Any]]:
    """Pull tool-call names and submit_categorization args from a RunResult."""
    tools: list[str] = []
    submit: dict[str, Any] = {}
    for message in getattr(result, "messages", []) or []:
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", None) == "tool_call":
                name = getattr(block, "name", "")
                tools.append(name)
                if name == "submit_categorization":
                    submit = dict(getattr(block, "arguments", {}) or {})
    return tools, submit


async def _run_agent_traced(agent: Any, prompt: str, *, session_id: str) -> Any:
    """Run the agent with full agent-harness tracing (model turns + tool calls)."""
    from agent_harness import InMemoryEventBus

    import penny.observability as observability

    bus = InMemoryEventBus()
    trace_task = observability.start_run_trace_task(
        bus,
        source="categorizer-eval",
        trace_name="categorizer-eval",
        session_id=session_id,
        prompt=prompt,
        tags=["categorizer-eval"],
    )
    try:
        return await agent.run(prompt=prompt, event_bus=bus)
    finally:
        await bus.close()
        if trace_task is not None:
            with contextlib.suppress(Exception):
                await trace_task


async def replay_one(txn: dict[str, Any]) -> dict[str, Any]:
    """Categorize one txn (fast path or agent) and return the captured decision.

    Persists on the current DB (the branch). Returns
    ``{method_at_eval_time, agent_key, agent_confidence, agent_reasoning,
    tools_consulted, trace_link}``.
    """
    from penny.db import get_db
    import penny.observability as observability
    from penny.tools._services import categorizer_agent
    from penny.tools._services.categorizer_agent import build_categorizer_agent

    db = get_db()
    descriptor = txn.get("merchant_descriptor") or ""
    verified = db.verified_category_for_descriptor(descriptor) if descriptor else None
    if verified:
        categorizer_agent._apply_fast_path(db, txn["transaction_id"], verified)
        return {
            "method_at_eval_time": "fast_path",
            "agent_key": verified,
            "agent_confidence": 1.0,
            "agent_reasoning": "Exact verified match for this merchant descriptor.",
            "tools_consulted": [],
            "trace_link": None,
        }

    tid = txn["transaction_id"]
    agent = build_categorizer_agent()
    trace_link: str | None = None
    with observability.categorizer_span(
        "categorizer-eval",
        input={"transaction_id": tid, "descriptor": descriptor},
        session_id=f"eval-{tid}",
        metadata={"harness": "categorizer_eval"},
    ):
        trace_link = observability.current_trace_url()
        result = await _run_agent_traced(
            agent, categorizer_agent._build_txn_prompt(txn), session_id=f"eval-{tid}"
        )
    tools, submit = _extract_from_result(result)
    return {
        "method_at_eval_time": "agent",
        "agent_key": submit.get("category_key"),
        "agent_confidence": submit.get("confidence"),
        "agent_reasoning": submit.get("reasoning"),
        "tools_consulted": tools,
        "trace_link": trace_link,
    }
