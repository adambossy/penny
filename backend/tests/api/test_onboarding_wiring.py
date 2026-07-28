"""The chat handler enqueues the consolidated onboarding reminder per turn."""

from __future__ import annotations

from penny.api.main import _maybe_enqueue_onboarding
from penny.api.persistence.reminders import DbReminderQueue
from tests.test_onboarding import _setup  # reuse schema creation


async def test_conversation_enqueues(isolated_db):
    _setup()
    await _maybe_enqueue_onboarding("conv-1")
    drained = await DbReminderQueue().drain("conv-1")
    assert len(drained) == 1 and drained[0].kind == "onboarding"
    assert "connect_plaid" in drained[0].content  # unlinked -> connect nudge
