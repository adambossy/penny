"""Daemon due-checks: interval sync, weekly report slot, retry-on-failure.

REQUIREMENTS P4 / the daemon docstring: jobs are skew-tolerant and a failed
run retries on the next tick — success is what advances the schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from penny.daemon import _due_report, _due_sync

_SCHEDULE = {"sync_interval_hours": 12, "report_weekday": 1, "report_hour": 8}

# A Monday (isoweekday 1) at 09:00 local.
_MONDAY_9AM = datetime(2026, 8, 3, 9, 0).astimezone()


def _state(name: str, *, ran_at: datetime, ok: bool) -> dict:
    return {name: {"last_run_at": ran_at.isoformat(), "ok": ok, "detail": ""}}


def test_sync_due_when_never_run():
    assert _due_sync({}, datetime.now(UTC), 12)


def test_sync_waits_out_the_interval_after_success():
    now = datetime.now(UTC)
    state = _state("sync", ran_at=now - timedelta(hours=1), ok=True)
    assert not _due_sync(state, now, 12)
    assert _due_sync(state, now + timedelta(hours=12), 12)


def test_sync_failure_retries_next_tick():
    now = datetime.now(UTC)
    state = _state("sync", ran_at=now - timedelta(minutes=1), ok=False)
    assert _due_sync(state, now, 12)


def test_report_fires_once_per_slot_on_success():
    assert _due_report({}, _MONDAY_9AM, _SCHEDULE)
    ran = _state("report", ran_at=_MONDAY_9AM - timedelta(minutes=30), ok=True)
    assert not _due_report(ran, _MONDAY_9AM, _SCHEDULE)
    # Outside the slot (wrong weekday) nothing fires.
    assert not _due_report({}, _MONDAY_9AM + timedelta(days=1), _SCHEDULE)


def test_report_failure_retries_while_slot_is_open():
    failed = _state("report", ran_at=_MONDAY_9AM - timedelta(minutes=30), ok=False)
    assert _due_report(failed, _MONDAY_9AM, _SCHEDULE)
