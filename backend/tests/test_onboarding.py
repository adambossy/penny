"""Deterministic onboarding trigger engine (``penny.api.persistence.onboarding``).

Onboarding items are website/app state in the ``web`` schema (decision D1), so
``ensure_items`` / ``evaluate`` take a web session rather than a finance session.
``_setup`` creates both schemas so downstream Plaid tests (which reuse it) can
write finance rows.
"""

from __future__ import annotations

from penny.api.persistence.engine import create_web_schema
from penny.api.persistence.models import OnboardingItem
from penny.api.persistence.onboarding import TurnSignals, ensure_items, evaluate
from penny.api.persistence.reminders import _web_session
from penny.db import get_db


def _setup() -> None:
    """Create the finance + web schemas on the isolated tmp DBs."""
    get_db().create_schema()
    create_web_schema()


def _signals(**kw) -> TurnSignals:
    base = {
        "has_linked_items": False,
        "response_had_categorized_rows": False,
        "user_corrected_category": False,
    }
    base.update(kw)
    return TurnSignals(**base)


def test_connect_plaid_fires_every_turn_until_resolved(isolated_db):
    _setup()
    with _web_session() as s:
        ensure_items(s)
        first = evaluate(s, _signals())
        second = evaluate(s, _signals())
    assert first and "connect_plaid" in first
    assert second and "connect_plaid" in second  # every turn, deterministic
    with _web_session() as s:  # dismiss -> silence
        s.query(OnboardingItem).filter_by(item_key="connect_plaid").update(
            {"status": "dismissed"}
        )
        assert evaluate(s, _signals()) is None


def test_custom_taxonomy_fires_after_three_categorized_turns(isolated_db):
    _setup()
    with _web_session() as s:
        ensure_items(s)
        s.query(OnboardingItem).filter_by(item_key="connect_plaid").update(
            {"status": "accepted"}
        )
        sig = _signals(has_linked_items=True, response_had_categorized_rows=True)
        assert evaluate(s, sig) is None  # turn 1
        assert evaluate(s, sig) is None  # turn 2
        third = evaluate(s, sig)  # turn 3 -> fires
    assert third and "custom_taxonomy" in third


def test_same_state_same_output(isolated_db):
    _setup()
    with _web_session() as s:
        ensure_items(s)
        a = evaluate(s, _signals())
    with _web_session() as s:
        b = evaluate(s, _signals())
    # deterministic template: the text before the first item key is identical
    assert a.split("connect_plaid")[0] == b.split("connect_plaid")[0]
