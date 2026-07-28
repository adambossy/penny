"""Deterministic progressive-onboarding engine (website/app domain).

Onboarding is website/app state (decision D1/D5): the items live in the ``web``
schema and this module owns the state machine + per-turn trigger evaluation. The
website chat handler calls :func:`ensure_items` + :func:`evaluate` each turn and
enqueues the returned consolidated reminder; the agent's
``resolve_onboarding_item`` tool calls :func:`resolve` on explicit accept/decline.

The engine is deterministic (spec §4): given the same stored state and signals it
returns the same activation set, so a reminder's content is a pure function of
state — never model- or client-derived text. Activation is *computed* here, never
stored; only ``status`` (``pending``/``accepted``/``dismissed``) persists.

:func:`resolve` is the website-owned op the agent's ``resolve_onboarding_item``
tool needs; the website passes it to ``build_agent`` (matching
``OnboardingResolver``) so the agent domain never imports this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from .models import OnboardingItem

# The v1 onboarding steps, in the fixed order they appear in a consolidated
# reminder. Kept concrete here (not injected) — the reusable trigger core is the
# evaluate()/rule shape, per the plan's modularization note.
ITEM_KEYS: tuple[str, ...] = (
    "connect_plaid",
    "custom_taxonomy",
    "merchant_rules",
)

_VALID_ACTIONS = ("accepted", "dismissed")

# One-line, state-derived guidance per item (server-generated, never verbatim
# echoed — the prompt tells the agent to paraphrase).
_GUIDANCE: dict[str, str] = {
    "connect_plaid": (
        "The user has no bank connected yet — offer to connect one. The "
        "connect_bank_account tool renders an inline Plaid card."
    ),
    "custom_taxonomy": (
        "The user has been categorizing for a few turns — offer to tailor the "
        "category taxonomy to how they think about their spending."
    ),
    "merchant_rules": (
        "Offer to set up a merchant rule so similar transactions categorize "
        "themselves automatically going forward."
    ),
}

_CLOSING = (
    "Nudge naturally, at most once this turn, without repeating earlier "
    "phrasing; call resolve_onboarding_item when the user accepts or declines."
)


@dataclass(frozen=True)
class TurnSignals:
    """Per-turn inputs the trigger rules read.

    ``conversation_id`` scopes the once-per-session cadence (an item stamps the
    conversation it last nudged in and stays quiet there afterward). Defaulted so
    tests can build signals without it; the wiring always passes the real id.
    """

    has_linked_items: bool
    response_had_categorized_rows: bool
    user_corrected_category: bool
    conversation_id: str = ""


def ensure_items(session: Session) -> None:
    """Idempotently seed a pending row per item key."""
    existing = {row.item_key for row in session.query(OnboardingItem.item_key).all()}
    for key in ITEM_KEYS:
        if key not in existing:
            session.add(
                OnboardingItem(
                    item_key=key,
                    status="pending",
                    trigger_state={},
                )
            )
    session.flush()


def evaluate(session: Session, signals: TurnSignals) -> str | None:
    """Advance counters and return the consolidated reminder, or ``None``.

    Deterministic: updates each pending item's ``trigger_state`` counters from
    ``signals``, computes which pending items' rules fire this turn, and returns
    one consolidated ``onboarding`` reminder body describing them (or ``None``
    when none fire). Once-per-session items stamp the conversation they nudged in.
    """
    pending = (
        session.query(OnboardingItem).filter(OnboardingItem.status == "pending").all()
    )
    by_key = {item.item_key: item for item in pending}

    fired: list[str] = []
    for key in ITEM_KEYS:  # fixed order → deterministic content
        item = by_key.get(key)
        if item is None:
            continue
        state = dict(item.trigger_state or {})
        _advance_counters(state, signals)
        if _fires(key, state, signals):
            state["last_nudged_conversation"] = signals.conversation_id
            fired.append(key)
        # Reassign (not in-place) so SQLAlchemy tracks the JSON change.
        item.trigger_state = state

    session.flush()
    if not fired:
        return None
    return _render(fired)


def resolve(item_key: str, action: str) -> dict[str, str]:
    """Set an item's status to ``accepted``/``dismissed``.

    Returns ``{item_key, status}`` on success, or ``{error}`` for an unknown key
    or action (a model mistake surfaces as recoverable tool output).
    Everything stays revisitable: a dismissed item is never nudged again, but the
    user can still ask and the agent performs the underlying action directly.
    """
    from .reminders import _web_session

    if item_key not in ITEM_KEYS:
        return {"error": f"unknown item_key {item_key!r}"}
    if action not in _VALID_ACTIONS:
        return {"error": f"action must be one of {_VALID_ACTIONS}, got {action!r}"}
    with _web_session() as s:
        item = (
            s.query(OnboardingItem)
            .filter(OnboardingItem.item_key == item_key)
            .one_or_none()
        )
        if item is None:
            item = OnboardingItem(item_key=item_key, trigger_state={})
            s.add(item)
        item.status = action
        item.updated_at = datetime.now()
    return {"item_key": item_key, "status": action}


def _advance_counters(state: dict[str, object], signals: TurnSignals) -> None:
    if signals.response_had_categorized_rows:
        state["categorized_turns"] = int(state.get("categorized_turns", 0)) + 1
    if signals.user_corrected_category:
        state["corrections"] = int(state.get("corrections", 0)) + 1


def _fires(key: str, state: dict[str, object], signals: TurnSignals) -> bool:
    if key == "connect_plaid":
        # Every turn while unlinked (no once-per-session guard).
        return not signals.has_linked_items
    # The remaining items are once per session: quiet in a conversation they've
    # already nudged in.
    if state.get("last_nudged_conversation") == signals.conversation_id:
        return False
    categorized = int(state.get("categorized_turns", 0))
    if key == "custom_taxonomy":
        return categorized >= 3
    if key == "merchant_rules":
        return int(state.get("corrections", 0)) >= 1 or categorized >= 10
    return False


def _render(fired: list[str]) -> str:
    lines = ["Onboarding status (system-managed — paraphrase, do not repeat verbatim):"]
    lines += [f"- {key}: {_GUIDANCE[key]}" for key in fired]
    lines.append(_CLOSING)
    return "\n".join(lines)
