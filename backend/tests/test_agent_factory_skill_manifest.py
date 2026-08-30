"""The skill manifest actually reaches the model, as a per-turn reminder.

The ``Skill`` tool's own description tells the model it already saw
name + description + when_to_use for every skill in its system prompt —
but nothing in agent-harness or Penny ever made that true (a scheduled
report run once guessed a plausible-but-wrong skill name as a result).
These tests pin the fix: :func:`format_skill_manifest` renders the
manifest, and :func:`announce_skill_manifest` delivers it through the
existing ``ReminderQueue`` mechanism (the same one that already carries
onboarding nudges into a turn).
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_harness.extras.reminders import InMemoryReminderQueue

from penny.agent_factory import announce_skill_manifest, format_skill_manifest


def _fake_registry(entries: list[dict[str, str]]) -> SimpleNamespace:
    """A stand-in for ``SkillRegistry`` — only ``.manifest()`` is used."""
    return SimpleNamespace(manifest=lambda: entries)


def test_format_skill_manifest_lists_every_skill_with_its_slug() -> None:
    # input: a manifest with two skills, mirroring SkillRegistry.manifest()'s
    # shape (name + description + when_to_use only).
    registry = _fake_registry(
        [
            {
                "name": "spending-report",
                "description": "Generate a spending report over a period.",
                "when_to_use": "The user asks for a spending summary.",
            },
            {
                "name": "budgets",
                "description": "Track and update the budget snapshot.",
                "when_to_use": "The user asks about their budget.",
            },
        ]
    )

    # act
    output = format_skill_manifest(registry)

    # expected: the real skill slug is literally present (this is exactly
    # what was missing when the model guessed "report-generation" instead).
    expected_lines = [
        "- spending-report: Generate a spending report over a period. - "
        "The user asks for a spending summary.",
        "- budgets: Track and update the budget snapshot. - "
        "The user asks about their budget.",
    ]

    # assert
    for line in expected_lines:
        assert line in output
    assert "spending-report" in output


async def test_announce_skill_manifest_noop_without_a_queue() -> None:
    # act / assert: no reminder queue provisioned (e.g. a bare context) means
    # nothing to enqueue into — must not raise.
    await announce_skill_manifest(None, "session-1", _fake_registry([]))


async def test_announce_skill_manifest_enqueues_under_its_own_kind() -> None:
    # input
    reminders = InMemoryReminderQueue()
    registry = _fake_registry(
        [{"name": "demo", "description": "x", "when_to_use": "y"}]
    )

    # act
    await announce_skill_manifest(reminders, "session-1", registry)
    drained = await reminders.drain("session-1")

    # expected: exactly one reminder, kind "skill_manifest", holding the
    # rendered manifest — this is what the run loop wraps in
    # <system-reminder> and flushes into the turn (agent_harness.core.loop).
    assert len(drained) == 1
    assert drained[0].kind == "skill_manifest"
    assert "demo" in drained[0].content


async def test_announce_skill_manifest_only_the_named_session_sees_it() -> None:
    # input: two sessions share one queue (the CLI/website both construct
    # their own, but this pins that draining is session-scoped either way).
    reminders = InMemoryReminderQueue()
    registry = _fake_registry(
        [{"name": "demo", "description": "x", "when_to_use": "y"}]
    )

    # act
    await announce_skill_manifest(reminders, "session-a", registry)

    # assert: an unrelated session drains nothing.
    assert await reminders.drain("session-b") == []
    # and the original session still has its reminder queued.
    assert len(await reminders.drain("session-a")) == 1
