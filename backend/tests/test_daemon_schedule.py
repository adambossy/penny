"""Daemon due-checks: interval sync, period-identity report jobs, retry-on-failure.

REQUIREMENTS P4 / the daemon docstring: jobs are skew-tolerant and a failed
run retries on the next tick — success is what advances the schedule. Report
jobs layer on the priority + max_emails_per_day cap: of the jobs due on one
tick, the top of the priority order sends; the rest resolve their current
period without sending (no backfill after an outage).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from penny import daemon
from penny.daemon import _due_reports, _due_sync, _send_report, _tick_reports
from penny.services.scheduled_reports import period_identity

# Monday 2026-08-03, 09:00 in New York (13:00 UTC during EDT).
_MONDAY_9AM = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
_TUESDAY_9AM = _MONDAY_9AM + timedelta(days=1)

# The daemon only reads the scheduling fields; recipients live in the job's
# own config and are applied by `run-scheduled-report` itself (see test_cli).
_DAILY = {"name": "daily", "period": "daily", "hour": 8, "priority": 1}
_WEEKLY = {"name": "weekly", "period": "weekly", "weekday": 1, "hour": 8, "priority": 2}


def _state(name: str, *, ran_at: datetime, ok: bool) -> dict:
    return {name: {"last_run_at": ran_at.isoformat(), "ok": ok, "detail": ""}}


@pytest.fixture(autouse=True)
def _in_memory_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the state dict under test authoritative — no state-file writes."""
    monkeypatch.setattr(daemon, "write_state", lambda state: None)


def _stub_run_job(
    monkeypatch: pytest.MonkeyPatch, *, ok: bool = True
) -> list[dict[str, Any]]:
    """Replace the subprocess runner; mimic its state write, capture calls."""
    calls: list[dict[str, Any]] = []

    def run(state: dict[str, Any], name: str, argv: list[str]) -> bool:
        calls.append({"name": name, "argv": argv})
        state[name] = {
            "last_run_at": datetime.now(UTC).isoformat(),
            "ok": ok,
            "detail": "",
        }
        return ok

    monkeypatch.setattr(daemon, "_run_job", run)
    return calls


# --- sync (interval-based; untouched by the report-job mechanism) ---


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


# --- report jobs: due set, priority + cap, catch-up, retry ---


def test_reports_due_when_never_resolved_sorted_by_priority():
    due = _due_reports({}, _MONDAY_9AM, [_DAILY, _WEEKLY])
    assert [job["name"] for job in due] == ["weekly", "daily"]


def test_report_not_due_once_its_period_is_resolved():
    state = {
        "report:daily": {
            "last_run_at": _MONDAY_9AM.isoformat(),
            "ok": True,
            "detail": "",
            "last_resolved_period": period_identity(_DAILY, now_utc=_MONDAY_9AM),
        }
    }
    assert _due_reports(state, _MONDAY_9AM, [_DAILY]) == []
    # The next day is a new period, so the job comes due again.
    assert _due_reports(state, _TUESDAY_9AM, [_DAILY]) == [_DAILY]


def test_cap_sends_highest_priority_and_resolves_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_run_job(monkeypatch)
    state: dict[str, Any] = {}

    _tick_reports(state, _MONDAY_9AM, [_DAILY, _WEEKLY], 1)

    # Only weekly (priority 2) ran.
    assert [c["name"] for c in calls] == ["report:weekly"]
    assert calls[0]["argv"] == ["run-scheduled-report", "--job", "weekly"]
    assert state["report:weekly"]["last_resolved_period"] == period_identity(
        _WEEKLY, now_utc=_MONDAY_9AM
    )
    # daily was resolved without sending: ok, cap-suppression detail names
    # the winner, and its current period is spent.
    suppressed = state["report:daily"]
    assert suppressed["ok"] is True
    assert "max_emails_per_day" in suppressed["detail"]
    assert "weekly" in suppressed["detail"]
    assert suppressed["last_resolved_period"] == period_identity(
        _DAILY, now_utc=_MONDAY_9AM
    )


def test_catch_up_after_outage_sends_only_the_top_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Laptop closed for weeks: both jobs' recorded periods are long stale.
    calls = _stub_run_job(monkeypatch)
    state: dict[str, Any] = {
        "report:daily": {
            "last_run_at": "2026-07-01T12:00:00+00:00",
            "ok": True,
            "detail": "",
            "last_resolved_period": "2026-07-01",
        },
        "report:weekly": {
            "last_run_at": "2026-06-29T12:00:00+00:00",
            "ok": True,
            "detail": "",
            "last_resolved_period": "2026-W27",
        },
    }

    _tick_reports(state, _MONDAY_9AM, [_DAILY, _WEEKLY], 1)

    # Only weekly sent — the missed occurrences do NOT backfill.
    assert [c["name"] for c in calls] == ["report:weekly"]
    # daily resolved for *today's* identity only, so nothing more is due
    # today, and daily fires again on its next natural day.
    assert state["report:daily"]["last_resolved_period"] == period_identity(
        _DAILY, now_utc=_MONDAY_9AM
    )
    assert _due_reports(state, _MONDAY_9AM, [_DAILY, _WEEKLY]) == []
    assert _due_reports(state, _TUESDAY_9AM, [_DAILY, _WEEKLY]) == [_DAILY]


def test_successful_send_persists_resolved_period_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash right after a successful send must not re-trigger the report.

    ``_run_job`` persists the bare "ok" run record on its own; if the resolved
    period lagged behind until ``_tick_reports``'s trailing ``write_state``, a
    daemon restart in between would see a successful run that still looks due
    and would send the report again. Every ``write_state`` call is captured
    so the last one observed before ``_send_report`` returns must already
    carry the resolved period.
    """
    _stub_run_job(monkeypatch)
    snapshots: list[dict[str, Any]] = []
    monkeypatch.setattr(
        daemon, "write_state", lambda state: snapshots.append({**state})
    )
    state: dict[str, Any] = {}

    _send_report(state, _WEEKLY, _MONDAY_9AM)

    assert snapshots, "a successful send must persist state"
    assert "last_resolved_period" in snapshots[-1]["report:weekly"]


def test_failed_send_does_not_advance_the_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run_job(monkeypatch, ok=False)
    state: dict[str, Any] = {}

    _tick_reports(state, _MONDAY_9AM, [_WEEKLY], 1)

    # Failure recorded, but the period was not resolved — the job stays due
    # and retries on the next tick.
    assert state["report:weekly"]["ok"] is False
    assert "last_resolved_period" not in state["report:weekly"]
    assert _due_reports(state, _MONDAY_9AM, [_WEEKLY]) == [_WEEKLY]
