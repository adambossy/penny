"""``report_job_states`` un-namespaces report-job entries out of daemon state.

Regression coverage: ``sync_status`` used to read a bare ``"report"`` key
that no longer exists now that report jobs are namespaced ``report:<name>``
(one entry per configured job), which silently made its ``last_report``
field always ``None``.
"""

from __future__ import annotations

from penny.daemon_state import REPORT_STATE_PREFIX, report_job_states


def test_report_job_states_extracts_namespaced_entries() -> None:
    state = {
        "sync": {"last_run_at": "2026-08-03T13:00:00+00:00", "ok": True},
        f"{REPORT_STATE_PREFIX}weekly": {
            "ok": True,
            "last_resolved_period": "2026-W31",
        },
        f"{REPORT_STATE_PREFIX}monthly": {
            "ok": False,
            "last_resolved_period": "2026-07",
        },
    }

    assert report_job_states(state) == {
        "weekly": {"ok": True, "last_resolved_period": "2026-W31"},
        "monthly": {"ok": False, "last_resolved_period": "2026-07"},
    }


def test_report_job_states_excludes_sync_and_is_empty_when_absent() -> None:
    assert report_job_states({"sync": {"ok": True}}) == {}
    assert report_job_states({}) == {}
