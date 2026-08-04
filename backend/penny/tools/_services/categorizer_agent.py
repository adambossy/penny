"""Per-transaction categorizer agent (agent-harness based).

This is the new categorization path: instead of a single-shot batch LLM call, each
transaction is decided by a short-lived, ephemeral agent that can consult history,
tags, the events log, and web_search before committing a decision via the final
``submit_categorization`` tool.

Two tiers:
- **Fast path (no LLM)** — an exact merchant descriptor that already has a VERIFIED
  categorization is reused at confidence 1.0 and the new row is marked verified.
- **Agent path** — everything else runs the agent loop.

The agent is built WITHOUT filesystem/bash/skills tools (capability confinement) and
runs in-process with its own per-run scratch sandbox under ``~/.transactoid``.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date
from typing import Any
import uuid

from agent_harness import Agent
from agent_harness.core.events import InMemoryEventBus
from agent_harness.core.models import ModelSettings
from agent_harness.providers.google import GeminiModel
from agent_harness.sandboxes.inprocess import InProcessSandbox

from penny import observability
from penny.agent_factory import AgentModel, _thinking_budget_from_env, build_model
from penny.config import categorizer_model
from penny.db import get_db
from penny.prompts import load_prompt
from penny.services import (
    get_rules_loader,
    get_taxonomy,
    get_taxonomy_rules_loader,
)
from penny.taxonomy.loader import get_category_id
from penny.tools._services.categorizer_tools import build_categorizer_toolset
from penny.workspace import resolve_workspace_dir

# Provider-native web_search for the agent. Passed via ``ModelSettings.builtin_tools``,
# which the harness appends to the wire tools list alongside the function tools (so the
# agent keeps submit_categorization etc.). Google's grounding tool, so it is attached
# only when the model is actually Gemini (see ``_categorizer_model_settings``). This
# coexists with function calling (the JSON-output vs grounding exclusivity only applies
# to response_mime_type, which we don't use).
_GEMINI_WEB_SEARCH_TOOL: dict[str, Any] = {"google_search": {}}


def _categorizer_model_settings(model: AgentModel) -> ModelSettings:
    """Settings that follow the model, not the historical Gemini assumption.

    ``categorizer_model()`` falls back to ``agent_model()``, so setting only
    ``PENNY_AGENT_MODEL`` re-points the categorizer too — the settings must
    track that. The thinking budget reuses the chat agent's dialect rule
    (``-1`` is Gemini's "dynamic" but is rejected as ``budget_tokens`` on the
    Anthropic-shaped OpenRouter path), and the Gemini-only grounding tool is
    attached only when the model is actually Gemini.
    """
    return ModelSettings(
        thinking_budget=_thinking_budget_from_env(model),
        builtin_tools=(
            [_GEMINI_WEB_SEARCH_TOOL] if isinstance(model, GeminiModel) else []
        ),
    )


def _format_recent_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    lines = []
    for event in events:
        when = (event.get("created_at") or "")[:10]
        descriptor = event.get("merchant_descriptor") or "?"
        frm = event.get("from_category_key") or "—"
        to = event.get("to_category_key") or "?"
        why = (
            event.get("recategorization_reason")
            or event.get("categorization_reasoning")
            or ""
        )
        why = why.replace("\n", " ")[:120]
        lines.append(
            f"- [{when}] {descriptor}: {frm} → {to} ({event.get('method')}) {why}"
        )
    return "\n".join(lines)


def _render_categorizer_prompt() -> str:
    """Render the categorizer agent's system prompt with live context.

    The taxonomy reaches the agent through a single ``{{TAXONOMY_RULES}}`` block:
    the workspace ``taxonomy-rules.md`` lists every category as ``Name (key)``
    with its definition and the cross-category decision logic. We no longer
    inject a separate hierarchy block — the keys are folded into the rules.
    """
    template = load_prompt("categorize-transaction-agent")
    taxonomy_rules = get_taxonomy_rules_loader().load()
    rules_loader = get_rules_loader()
    merchant_rules = (
        rules_loader.load() if rules_loader else ""
    ) or "(no merchant rules)"
    recent_block = (
        _format_recent_events(get_db().recent_category_events(limit=15)) or "(none yet)"
    )

    rendered = template.replace("{{CURRENT_DATE}}", date.today().isoformat())
    rendered = rendered.replace("{{TAXONOMY_RULES}}", taxonomy_rules)
    rendered = rendered.replace("{{MERCHANT_RULES}}", merchant_rules)
    rendered = rendered.replace("{{RECENT_EVENTS}}", recent_block)
    return rendered


def build_categorizer_agent() -> Agent:
    """Build a fresh, ephemeral categorizer agent with its own scratch sandbox."""
    run_id = uuid.uuid4().hex
    scratch_root = resolve_workspace_dir() / "agent-runs" / run_id
    sandbox = InProcessSandbox(root=str(scratch_root))

    model = build_model(name=categorizer_model())
    return Agent(
        name="categorizer",
        model=model,
        instructions=_render_categorizer_prompt(),
        session=None,
        persist_session=False,
        sandbox=sandbox,
        model_settings=_categorizer_model_settings(model),
        toolsets=[build_categorizer_toolset()],
    )


def _build_txn_prompt(txn: dict[str, Any]) -> str:
    amount = txn.get("amount")
    amount_str = f"${amount:.2f}" if isinstance(amount, (int, float)) else "unknown"
    lines = [
        "Categorize this single transaction. Pass the exact transaction_id below to "
        "submit_categorization when you are done.\n",
        f"- transaction_id: {txn['transaction_id']}",
        # Label kept as "merchant descriptor" so it matches the string the agent's
        # history/fast-path tools key on (merchant_category_history(descriptor)).
        f"- merchant descriptor: {txn.get('merchant_descriptor') or '(none)'}",
    ]
    # Plaid's raw `name` is the fuller, uncleaned descriptor — it often carries
    # location or payment-rail detail the cleaned merchant descriptor drops. Only
    # show it when it adds something (present and not identical to the descriptor).
    raw_name = (txn.get("raw_name") or "").strip()
    merchant = (txn.get("merchant_descriptor") or "").strip()
    if raw_name and raw_name.lower() != merchant.lower():
        lines.append(f"- raw bank descriptor: {raw_name}")
    lines.append(f"- amount: {amount_str}")
    lines.append(f"- date: {txn.get('date') or 'unknown'}")
    return "\n".join(lines) + "\n"


def _apply_fast_path(db: Any, transaction_id: int, category_key: str) -> None:
    """Reuse a verified category for an exact descriptor; mark the row verified."""
    category_id = get_category_id(db, get_taxonomy(), category_key)
    if category_id is None:
        raise ValueError(f"Category ID not found for key: {category_key!r}")
    db.update_derived_mutable(
        transaction_id,
        {
            "category_id": category_id,
            "category_method": "manual",
            "category_reason": (
                "Fast path: reused an existing verified categorization for this "
                "exact merchant descriptor."
            ),
            "is_verified": True,
        },
    )


def _read_decision(db: Any, transaction_id: int) -> dict[str, Any] | None:
    events = db.events_for_transaction(transaction_id)
    if not events:
        return None
    latest = events[-1]
    return {
        "transaction_id": transaction_id,
        "category_key": latest.get("to_category_key"),
        "reasoning": latest.get("categorization_reasoning"),
        "method": latest.get("method"),
    }


# Substrings that mark a *transient* model failure worth retrying — under sweep
# concurrency Gemini returns 503/UNAVAILABLE/429 often, and a single one used to
# abort the whole categorization batch.
_TRANSIENT_MARKERS = ("503", "unavailable", "429", "resource_exhausted", "overloaded")


async def _run_categorizer_with_retry(
    prompt: str, bus: Any, *, attempts: int = 4
) -> None:
    """Run the categorizer agent, retrying transient model errors with backoff.

    Rebuilds the agent per attempt (cheap) so a failed run doesn't carry state
    forward. A non-transient error, or the final attempt, propagates — the sweep
    then leaves that row for the next (cursor-independent) run.
    """
    delay = 1.0
    for attempt in range(attempts):
        agent = build_categorizer_agent()
        try:
            await agent.run(prompt, event_bus=bus)
            return
        except Exception as exc:  # noqa: BLE001 - classify then re-raise
            transient = any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)
            if not transient or attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20.0)


async def categorize_one(txn: dict[str, Any]) -> dict[str, Any]:
    """Categorize one derived transaction.

    Args:
        txn: dict with at least ``transaction_id`` and ``merchant_descriptor``
            (``amount`` / ``date`` are used to enrich the agent prompt).

    Returns:
        A decision dict ``{transaction_id, category_key, confidence?, method, reasoning}``.
        ``method`` is ``"fast_path"`` when the verified-match shortcut fired, else the
        agent's decision (read back from the persisted event).
    """
    db = get_db()
    transaction_id = txn["transaction_id"]
    descriptor = txn.get("merchant_descriptor") or ""

    if descriptor:
        verified_key = db.verified_category_for_descriptor(descriptor)
        if verified_key:
            _apply_fast_path(db, transaction_id, verified_key)
            return {
                "transaction_id": transaction_id,
                "category_key": verified_key,
                "confidence": 1.0,
                "method": "fast_path",
                "reasoning": (f"Exact verified match for descriptor {descriptor!r}."),
            }

    # Trace the categorizer agent to Langfuse via the same OTEL subscriber the
    # chat/cron paths use — one trace per transaction. The migration to this
    # agent (PR #49) dropped the old categorizer_span wiring, so categorization
    # failures (the agent not calling submit_categorization) were invisible.
    # No-op when tracing is disabled (bus is None → start_run_trace_task returns
    # None). The short-lived `penny sync` CLI flushes spans before it exits.
    bus = InMemoryEventBus() if observability.is_enabled() else None
    trace_task = observability.start_run_trace_task(
        bus,
        source="categorizer",
        trace_name="penny-categorizer",
        session_id=f"categorizer-{transaction_id}",
        prompt=descriptor or _build_txn_prompt(txn),
        tags=["categorizer"],
    )
    try:
        await _run_categorizer_with_retry(_build_txn_prompt(txn), bus)
    finally:
        if bus is not None:
            await bus.close()
        if trace_task is not None:
            with contextlib.suppress(Exception):
                await trace_task
    # submit_categorization persisted the decision; read it back for the caller.
    decision = _read_decision(db, transaction_id)
    if decision is None:
        return {
            "transaction_id": transaction_id,
            "category_key": None,
            "method": "agent",
            "reasoning": "Agent did not submit a categorization.",
        }
    return decision
