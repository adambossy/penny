"""DB-backed reminder queue (``penny.api.persistence.reminders.DbReminderQueue``).

The queue is website/app state in the ``web`` schema (decision D1), so tests
create the web schema before driving the queue.
"""

from __future__ import annotations

from penny.api.persistence.engine import create_web_schema
from penny.api.persistence.reminders import DbReminderQueue


def _queue() -> DbReminderQueue:
    create_web_schema()
    return DbReminderQueue()


async def test_override_upserts_and_drain_deletes(isolated_db):
    q = _queue()
    await q.enqueue("conv-1", "onboarding", "v1")
    await q.enqueue("conv-1", "onboarding", "v2")  # override -> single row
    await q.enqueue("conv-1", "plaid_link", "linked")
    drained = await q.drain("conv-1")
    assert [(r.kind, r.content) for r in drained] == [
        ("onboarding", "v2"),
        ("plaid_link", "linked"),
    ]
    assert await q.drain("conv-1") == []  # drained empty


async def test_no_override_appends(isolated_db):
    q = _queue()
    await q.enqueue("conv-1", "note", "a", override=False)
    await q.enqueue("conv-1", "note", "b", override=False)
    assert [r.content for r in await q.drain("conv-1")] == ["a", "b"]
    # non-override kinds still drain under the bare kind (suffix stripped)
    assert await q.drain("conv-1") == []


async def test_queues_are_per_conversation(isolated_db):
    q = _queue()
    await q.enqueue("conv-a", "onboarding", "a-state")
    assert await q.drain("conv-b") == []
    assert [r.content for r in await q.drain("conv-a")] == ["a-state"]
